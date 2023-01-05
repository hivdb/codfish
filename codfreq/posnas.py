import pysam  # type: ignore
import cython  # type: ignore
from tqdm import tqdm  # type: ignore
from pysam import AlignedSegment  # type: ignore
from typing import List, Tuple, Optional, Generator, Any, Union
from concurrent.futures import ProcessPoolExecutor

from .json_progress import JsonProgress
from .samfile_helper import chunked_samfile
from .codfreq_types import NAPos, NAChar, SeqText, Header

ENCODING: str = 'UTF-8'
GAP: int = cython.declare(cython.char, ord(b'-'))


@cython.cclass
class PosNA:
    """A class to represent a nucleotide position (pos), insertion gap offset
    (bp) and the nucleotide (na).

    :var pos: The position of the nucleotide in the alignment.
    :var bp: The insertion gap offset of the nucleotide relative to `pos`.
    :var na: The single nucleotide ("ACGT") or a deletion gap ("-").
    """

    pos: NAPos = cython.declare(cython.ulong, visibility="public")
    bp: int = cython.declare(cython.uint, visibility="public")
    na: NAChar = cython.declare(cython.char, visibility="public")

    def __init__(self: 'PosNA', pos: NAPos, bp: int, na: NAChar):
        self.pos = pos
        self.bp = bp
        self.na = na

    def __hash__(self: 'PosNA') -> int:
        return hash((self.pos, self.bp, self.na))

    def __eq__(self: 'PosNA', other: Any) -> bool:
        if not isinstance(other, PosNA):
            return False
        return (self.pos, self.bp, self.na) == (other.pos, other.bp, other.na)

    def __repr__(self: 'PosNA') -> str:
        return f"PosNA({self.pos!r}, {self.bp!r}, {self.na!r})"


@cython.ccall
@cython.inline
@cython.returns(list)
def iter_single_read_posnas(
    seq: SeqText,
    aligned_pairs: List[Tuple[Optional[NAPos], Optional[NAPos]]]
) -> List[PosNA]:
    seqpos0: Optional[NAPos]
    refpos0: Optional[NAPos]
    refpos: NAPos = cython.declare(cython.ulong)

    insidx: int = cython.declare(cython.uint, 0)
    n: NAChar = cython.declare(cython.char)
    seqchars: bytes = bytes(seq, ENCODING)
    prev_refpos: int = cython.declare(cython.ulong, 0)
    buffer_size: int = cython.declare(cython.uint, 0)
    posnas: List[PosNA] = []

    for seqpos0, refpos0 in aligned_pairs:

        if refpos0 is None:
            # insertion
            refpos = prev_refpos
            insidx += 1
        else:
            refpos = refpos0 + 1
            insidx = 0
            prev_refpos = refpos

        if seqpos0 is None:
            # deletion
            n = GAP
        else:
            n = seqchars[seqpos0]

        if refpos == 0:
            # insertion before the first ref position
            continue

        posnas.append(PosNA(refpos, insidx, n))

        if insidx > 0:
            buffer_size += 1
        else:
            buffer_size = 0

    return posnas[:len(posnas) - buffer_size]


@cython.ccall
@cython.locals(
    samfile_start=cython.ulong,
    samfile_end=cython.ulong
)
@cython.returns(list)
def get_posnas_between(
    samfile: str,
    samfile_start: int,
    samfile_end: int
) -> List[Tuple[Optional[Header], List[PosNA]]]:
    read: AlignedSegment
    posnas: List[PosNA]

    results: List[Tuple[Optional[Header], List[PosNA]]] = []

    with pysam.AlignmentFile(samfile, 'rb') as samfp:

        samfp.seek(samfile_start)

        for read in samfp:
            if samfp.tell() > samfile_end:
                break

            if not read.query_sequence:
                continue

            posnas = iter_single_read_posnas(
                read.query_sequence,
                read.get_aligned_pairs(False)
            )
            results.append((read.query_name, posnas))

    return results


@cython.ccall
@cython.locals(
    ref_start=cython.ulong,
    ref_end=cython.ulong
)
@cython.returns(list)
def get_posnas_in_genome_region(
    samfile: str,
    ref_name: str,
    ref_start: NAPos,
    ref_end: NAPos
) -> List[Tuple[Optional[Header], List[PosNA]]]:

    pos: NAPos
    idx: int
    na: NAChar
    q: int
    read: AlignedSegment
    posnas: List[PosNA]

    results: List[Tuple[Optional[Header], List[PosNA]]] = []

    with pysam.AlignmentFile(samfile, 'rb') as samfp:

        for read in samfp.fetch(ref_name, ref_start - 1, ref_end):

            if not read.query_sequence:
                continue

            posnas = iter_single_read_posnas(
                read.query_sequence,
                read.get_aligned_pairs(False)
            )
            results.append((read.query_name, posnas))

    return results


def iter_posnas(
    samfile: str,
    workers: int,
    description: str,
    jsonop: str = 'progress',
    chunk_size: int = 5000,
    log_format: str = 'text',
    **extras: Any
) -> Generator[
    Tuple[Header, List[PosNA]],
    None,
    None
]:
    total: int
    header: Header
    chunks: List[Tuple[int, int]]
    samfile_begin: int
    samfile_end: int
    one: Tuple[Header, List[PosNA]]
    posnas: List[Tuple[Header, List[PosNA]]]
    pbar: Optional[Union[JsonProgress, tqdm]] = None

    with pysam.AlignmentFile(samfile, 'rb') as samfp:
        total = samfp.mapped
        if log_format == 'json':
            pbar = JsonProgress(
                op=jsonop,
                total=total,
                description=description,
                **extras)
        elif log_format == 'text':
            pbar = tqdm(total=total)
            pbar.set_description('Processing {}'.format(description))

    chunks = chunked_samfile(samfile, chunk_size)

    with ProcessPoolExecutor(workers) as executor:

        for posnas in executor.map(
            get_posnas_between,
            *zip(*[
                (
                    samfile,
                    samfile_begin,
                    samfile_end
                )
                for samfile_begin, samfile_end in chunks
            ])
        ):
            if pbar:
                for one in posnas:
                    yield one
                    pbar.update(1)
            else:
                yield from posnas
    if pbar:
        pbar.close()
