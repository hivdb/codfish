import re
from collections import defaultdict, Counter
from .paired_reads import iter_paired_reads
from .poscodons import iter_poscodons
from .codonutils import translate_codon

CODON_PATTERN = re.compile(r'^[ACGTYRWSKMBDHVN]{3}$')


def sam2codfreq(sampath):
    codonfreqs = defaultdict(Counter)
    qualities = defaultdict(lambda: defaultdict(list))
    all_paired_reads = iter_paired_reads(sampath)
    for _, poscodons in iter_poscodons(all_paired_reads):
        for refpos, codon, qua in poscodons:
            codonfreqs[refpos][codon] += 1
            qualities[refpos][codon].append(qua)
    for refpos, codons in sorted(codonfreqs.items()):
        total = sum(codons.values())
        for codon, count in codons.items():
            qua = qualities[refpos][codon]
            qua = sum(qua) / len(qua)
            if CODON_PATTERN.match(codon):
                aa = translate_codon(codon)
            elif not codon or codon == '---':
                aa = 'del'
            elif len(codon) > 3:
                aa = 'ins'
            else:
                aa = 'X'
            yield {
                'position': refpos,
                'total': total,
                'codon': codon,
                'aa': aa,
                'count': count,
                'percent': count / total,
                'mean_quality_score': qua
            }
