#! /usr/bin/env python

import os
import re
import csv
import json
import click  # type: ignore
import tempfile
import multiprocessing
from itertools import combinations
from collections import defaultdict
from typing import (
    TextIO,
    Iterable,
    Generator,
    List,
    Tuple,
    DefaultDict,
    Set,
    Optional
)

from .codfreq_types import Profile, PairedFASTQ, CodFreqRow

from .sam2codfreq import (
    sam2codfreq_all,
    CODFREQ_HEADER
)
from .sam2consensus import create_untrans_region_consensus
from .cmdwrappers import (
    fastp, cutadapt, ivar, get_programs, get_refinit, get_align
)
from .filename_helper import (
    suggest_pair_name,
    name_bamfile,
    name_codfreq
)

ENCODING = 'UTF-8'
REQUIRED_PROFILE_VERSION = '20221213'
FILENAME_DELIMITERS = (' ', '_', '-')
PAIRED_FASTQ_MARKER = ('1', '2')
INVALID_PAIRED_FASTQ_MARKER = re.compile(r'[1-9]0*[12]|[^0]00+[12]|[12]\d')


def find_paired_marker(text1: str, text2: str) -> int:
    pos: int
    a: str
    b: str
    diffcount: int = 0
    diffpos: int = -1
    if (
        INVALID_PAIRED_FASTQ_MARKER.search(text1) or
        INVALID_PAIRED_FASTQ_MARKER.search(text2)
    ):
        return -1

    for pos, (a, b) in enumerate(zip(text1, text2)):
        if diffcount > 1:
            return -1
        if a == b:
            continue
        if a not in PAIRED_FASTQ_MARKER or b not in PAIRED_FASTQ_MARKER:
            return -1
        diffcount += 1
        diffpos = pos
    return diffpos


def find_paired_fastq_patterns(
    filenames: List[str],
    autopairing: bool
) -> Generator[PairedFASTQ, None, None]:
    """Smartly find paired FASTQ file patterns

    A valid filename pattern must meet:
    - use one of the valid delimiters (" ", "_" or "-") to separate the
      filename into different chunks
    - in one and only one chunk, a fixed position character changed from "1" to
      "2"

    Valid pair pattern examples:
      14258F_L001_R1_001.fastq.gz <-> 14258F_L001_R2_001.fastq.gz
      SampleExample_1.fastq <-> SampleExample_2.fastq

    Invalid pair pattern examples:
      SampleExample1.fastq <-> SampleExample2.fastq
      SampleExample_1.fastq <-> SampleExample_2.fastq.gz
      SampleExample_1.FASTQ.GZ <-> SampleExample_2.fastq.gz

    """
    left: str
    right: str
    invalid: bool
    delimiter: str
    diffcount: int
    diffoffset: int
    reverse: int
    pattern: Tuple[
        str,  # delimiter
        int,  # diffoffset
        int,  # pos_paired_marker
        int,  # reverse
    ]
    pairs: List[Tuple[str, str]]
    patterns: DefaultDict[
        Tuple[
            str,  # delimiter
            int,  # diffoffset
            int,  # pos_paired_marker
            int,  # reverse
        ],
        List[Tuple[str, str]]
    ] = defaultdict(list)
    if autopairing:
        for fn1, fn2 in combinations(filenames, 2):
            if len(fn1) != len(fn2):
                continue
            for delimiter in FILENAME_DELIMITERS:
                if delimiter not in fn1 or delimiter not in fn2:
                    continue
                chunks1: List[str] = fn1.split(delimiter)
                chunks2: List[str] = fn2.split(delimiter)
                if len(chunks1) != len(chunks2):
                    continue
                for reverse in range(2):
                    diffcount = 0
                    diffoffset = -1
                    invalid = False
                    if reverse:
                        chunks1.reverse()
                        chunks2.reverse()
                    for n, (left, right) in enumerate(zip(chunks1, chunks2)):
                        if diffcount > 1:
                            invalid = True
                            break
                        if left == right:
                            continue
                        pos_paired_marker: int = \
                            find_paired_marker(left, right)
                        if pos_paired_marker < 0:
                            invalid = True
                            break
                        diffoffset = n
                        diffcount += 1
                    if not invalid:
                        if fn1 > fn2:
                            # sort by filename
                            fn1, fn2 = fn2, fn1
                        patterns[(
                            delimiter,
                            diffoffset,
                            pos_paired_marker,
                            reverse
                        )].append((fn1, fn2))
    covered: Set[str] = set()
    if autopairing:
        for pattern, pairs in sorted(
                patterns.items(), key=lambda p: (-len(p[1]), -p[0][3])):
            known: Set[str] = set()
            invalid = False
            for left, right in pairs:
                if left in covered or right in covered:
                    # a pattern is invalid if the pairs is already matched
                    # by a previous pattern
                    invalid = True
                    break

                if left in known or right in known:
                    # a pattern is invalid if there's duplicate in pairs
                    invalid = True
                    break
                known.add(left)
                known.add(right)

            if not invalid:
                covered |= known
                for pair in pairs:
                    yield {
                        'name': suggest_pair_name(pair, pattern),
                        'pair': pair,
                        'n': 2
                    }
    if len(filenames) > len(covered):
        remains: List[str] = sorted(set(filenames) - covered)
        pattern = ('', -1, -1, -1)
        for left in remains:
            yield {
                'name': suggest_pair_name((left, None), pattern),
                'pair': (left, None),
                'n': 1
            }


def complete_paired_fastqs(
    paired_fastqs: Iterable[PairedFASTQ],
    dirpath: str
) -> Generator[PairedFASTQ, None, None]:
    for pairobj in paired_fastqs:
        yield {
            'name': os.path.join(dirpath, pairobj['name']),
            'pair': (
                os.path.join(dirpath, pairobj['pair'][0]),
                os.path.join(dirpath, pairobj['pair'][1])
                if pairobj['pair'][1] else None
            ),
            'n': pairobj['n']
        }


def find_paired_fastqs(
    workdir: str,
    autopairing: bool
) -> Generator[PairedFASTQ, None, None]:
    pairinfo: str = os.path.join(workdir, 'pairinfo.json')
    if os.path.isfile(pairinfo):
        with open(pairinfo) as fp:
            yield from complete_paired_fastqs(
                json.load(fp),
                workdir
            )
    else:
        pairinfo_list: List[PairedFASTQ] = []
        for dirpath, _, filenames in os.walk(workdir, followlinks=True):
            filenames = [
                fn for fn in filenames
                if (
                    fn[-6:].lower() == '.fastq'
                    or fn[-9:].lower() == '.fastq.gz'
                ) and not (
                    fn[-12:].lower() == 'merged.fastq'
                    or fn[-15:].lower() == 'merged.fastq.gz'
                )
            ]
            rel_dirpath = os.path.relpath(dirpath, workdir)
            pairinfo_list.extend(complete_paired_fastqs(
                find_paired_fastq_patterns(filenames, autopairing),
                rel_dirpath
            ))
        with open(pairinfo, 'w') as fp:
            json.dump(pairinfo_list, fp, indent=2)
        yield from complete_paired_fastqs(pairinfo_list, workdir)


def fastp_preprocess(
    paired_fastq: PairedFASTQ,
    fastp_config: fastp.FASTPConfig,
    log_format: str
) -> PairedFASTQ:
    if log_format == 'text':
        click.echo(
            'Pre-processing {} using fastp...'
            .format(paired_fastq['name'])
        )
    else:
        click.echo(json.dumps({
            'op': 'preprocess',
            'status': 'working',
            'query': paired_fastq['name']
        }))
    merged_fastq: PairedFASTQ = {
        'name': paired_fastq['name'],
        'pair': (
            os.path.join(
                os.path.dirname(paired_fastq['pair'][0]),
                '{}.merged.fastq.gz'.format(paired_fastq['name'])
            ),
            None
        ),
        'n': 1
    }
    fastp.fastp(
        paired_fastq['pair'][0],
        paired_fastq['pair'][1],
        merged_fastq['pair'][0],
        **fastp_config
    )
    if log_format == 'text':
        click.echo('Done')
    else:
        click.echo(json.dumps({
            'op': 'preprocess',
            'status': 'done',
            'query': paired_fastq['name']
        }))
    return merged_fastq


def ivar_trim(
    input_bam: str,
    output_bam: str,
    ivar_trim_config: ivar.TrimConfig,
    log_format: str
) -> None:
    name: str = os.path.basename(input_bam)
    if log_format == 'text':
        click.echo(
            'Trimming {} using ivar...'
            .format(name)
        )
    else:
        click.echo(json.dumps({
            'op': 'trim',
            'status': 'working',
            'command': 'ivar',
            'query': name
        }))
    ivar.trim(
        input_bam,
        output_bam,
        **ivar_trim_config
    )
    if log_format == 'text':
        click.echo('Done')
    else:
        click.echo(json.dumps({
            'op': 'trim',
            'status': 'done',
            'command': 'ivar',
            'query': name
        }))


def cutadapt_trim(
    merged_fastq: PairedFASTQ,
    cutadapt_config: cutadapt.CutadaptConfig,
    log_format: str
) -> PairedFASTQ:
    name: str = merged_fastq['name']
    output_fastq: PairedFASTQ = {
        'name': merged_fastq['name'],
        'pair': (
            os.path.join(
                os.path.dirname(merged_fastq['pair'][0]),
                '{}.merged-trimed.fastq.gz'.format(merged_fastq['name'])
            ),
            None
        ),
        'n': 1
    }
    if log_format == 'text':
        click.echo(
            'Trimming {} using cutadapt...'
            .format(name)
        )
    else:
        click.echo(json.dumps({
            'op': 'trim',
            'status': 'working',
            'command': 'cutadapt',
            'query': name
        }))
    cutadapt.cutadapt(
        merged_fastq['pair'][0],
        output_fastq['pair'][0],
        **cutadapt_config
    )
    if log_format == 'text':
        click.echo('Done')
    else:
        click.echo(json.dumps({
            'op': 'trim',
            'status': 'done',
            'command': 'cutadapt',
            'query': name
        }))
    return output_fastq


def align_with_profile(
    paired_fastq: PairedFASTQ,
    program: str,
    profile: Profile,
    log_format: str,
    fastp_config: fastp.FASTPConfig,
    cutadapt_config: Optional[cutadapt.CutadaptConfig],
    ivar_trim_config: Optional[ivar.TrimConfig]
) -> None:
    paired_fastq = fastp_preprocess(paired_fastq, fastp_config, log_format)

    if cutadapt_config is not None:
        paired_fastq = cutadapt_trim(
            paired_fastq, cutadapt_config, log_format)

    with tempfile.TemporaryDirectory('codfreq') as tmpdir:
        refpath = os.path.join(tmpdir, 'ref.fas')
        refinit = get_refinit(program)
        alignfunc = get_align(program)
        for config in profile['fragmentConfig']:
            if 'refSequence' not in config:
                continue
            refname = config['fragmentName']
            refseq = config['refSequence']
            with open(refpath, 'w') as fp:
                fp.write('>{}\n{}\n\n'.format(refname, refseq))

            orig_bamfile = name_bamfile(
                paired_fastq['name'],
                refname,
                is_trimmed=False)
            trimmed_bamfile = name_bamfile(
                paired_fastq['name'],
                refname,
                is_trimmed=True)
            refinit(refpath)
            if log_format == 'text':
                click.echo(
                    'Aligning {} with {}...'
                    .format(paired_fastq['name'], refname)
                )
            else:
                click.echo(json.dumps({
                    'op': 'alignment',
                    'status': 'working',
                    'query': paired_fastq['name'],
                    'target': refname
                }))
            alignfunc(refpath, *paired_fastq['pair'], orig_bamfile)
            if log_format == 'text':
                click.echo('Done')
            else:
                click.echo(json.dumps({
                    'op': 'alignment',
                    'status': 'done',
                    'query': paired_fastq['name'],
                    'target': refname
                }))
            if ivar_trim_config is None:
                os.replace(orig_bamfile, trimmed_bamfile)
                os.replace(orig_bamfile + '.bai', trimmed_bamfile + '.bai')
                os.replace(orig_bamfile + '.minimap2.log',
                           trimmed_bamfile + '.minimap2.log')
            else:
                ivar_trim(
                    orig_bamfile,
                    trimmed_bamfile,
                    ivar_trim_config,
                    log_format)


def align(
    workdir: str,
    program: str,
    profile: TextIO,
    workers: int,
    log_format: str,
    autopairing: bool
) -> None:
    row: CodFreqRow
    profile_obj: Profile = json.load(profile)
    if profile_obj.get('version') != REQUIRED_PROFILE_VERSION:
        click.echo(
            'Incompatible profile detected. Download the latest profile files '
            'from: https://github.com/hivdb/codfreq/tree/main/profiles',
            err=True)
        raise click.Abort()
    paired_fastqs = list(find_paired_fastqs(workdir, autopairing))

    fastp_config: fastp.FASTPConfig = fastp.load_config(
        os.path.join(workdir, 'fastp-config.json')
    )
    cutadapt_config: Optional[cutadapt.CutadaptConfig] = cutadapt.load_config(
        os.path.join(workdir, 'cutadapt-config.json'),
        adapter3_path=os.path.join(workdir, 'primers3.fa'),
        adapter5_path=os.path.join(workdir, 'primers5.fa'),
        adapter53_path=os.path.join(workdir, 'primers53.fa')
    )
    ivar_trim_config: Optional[ivar.TrimConfig] = ivar.load_trim_config(
        os.path.join(workdir, 'ivar-trim-config.json'),
        os.path.join(workdir, 'primers.bed')
    )

    for pairobj in paired_fastqs:
        align_with_profile(
            pairobj,
            program,
            profile_obj,
            log_format,
            fastp_config=fastp_config,
            cutadapt_config=cutadapt_config,
            ivar_trim_config=ivar_trim_config
        )
        codfreqfile = name_codfreq(pairobj['name'])
        with open(codfreqfile, 'w', encoding='utf-8-sig') as fp:
            writer = csv.DictWriter(fp, CODFREQ_HEADER)
            writer.writeheader()
            for row in sam2codfreq_all(
                name=pairobj['name'],
                fnpair=pairobj['pair'],
                profile=profile_obj,
                workers=workers,
                log_format=log_format
            ):
                writer.writerow({
                    **row,
                    'codon': row['codon'].decode(ENCODING)
                })

        create_untrans_region_consensus(
            pairobj['name'],
            profile_obj
        )


@click.command()
@click.argument(
    'workdir',
    type=click.Path(exists=True, file_okay=False,
                    dir_okay=True, resolve_path=True))
@click.option(
    '-p', '--program',
    required=True,
    type=click.Choice(get_programs()))
@click.option(
    '-r', '--profile',
    required=True,
    type=click.File('r', encoding=ENCODING))
@click.option(
    '--log-format',
    type=click.Choice(['text', 'json']),
    default='text', show_default=True)
@click.option(
    '--enable-profiling/--disable-profiling',
    default=False,
    help='Enable/disable cProfile')
@click.option(
    '--autopairing/--no-autopairing',
    default=True,
    help='Enable/disable automatical FASTQ pairing algorithm')
@click.option(
    '--workers',
    type=int,
    default=multiprocessing.cpu_count(),
    show_default=True,
    help='Number of sub-process workers to be used')
def align_cmd(
    workdir: str,
    program: str,
    profile: TextIO,
    log_format: str,
    enable_profiling: bool,
    autopairing: bool,
    workers: int
) -> None:
    if enable_profiling:
        import cProfile
        import pstats
        profile_obj = None
        try:
            with cProfile.Profile() as profile_obj:
                align(workdir, program, profile, workers,
                      log_format, autopairing)
        finally:
            if profile_obj is not None:
                ps = pstats.Stats(profile_obj)
                ps.print_stats()
    else:
        align(workdir, program, profile, workers, log_format, autopairing)


if __name__ == '__main__':
    align_cmd()
