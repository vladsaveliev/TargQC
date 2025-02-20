import os
import sys
from os.path import dirname, join, abspath, isfile, pardir
from pybedtools import BedTool
from targqc.utilz.file_utils import which, open_gzipsafe, verify_file
from targqc.utilz.logger import debug, critical

SUPPORTED_GENOMES = ['GRCh37', 'hg19', 'hg19-noalt', 'hg38', 'hg38-noalt', 'mm10', 'hg19-chr21']

class BedCols:
    CHROM, \
    START, \
    END, \
    GENE, \
    EXON, \
    STRAND, \
    FEATURE, \
    BIOTYPE, \
    ENSEMBL_ID, \
    TSL, \
    TX_OVERLAP_PERCENTAGE, \
    EXON_OVERLAPS_PERCENTAGE, \
    CDS_OVERLAPS_PERCENTAGE, \
    ORIGINAL_FIELDS \
        = cols = range(14)

    names = {
        CHROM: '#chrom',
        START: 'start',
        END: 'end',
        GENE: 'gene',
        EXON: 'exon',
        STRAND: 'strand',
        FEATURE: 'feature',
        BIOTYPE: 'biotype',
        ENSEMBL_ID: 'ens_id',
        TSL: 'tsl',
        # TX_OVERLAP_BASES: 'Tx_overlap_bp',
        TX_OVERLAP_PERCENTAGE: 'tx_overlap_pct',
        # EXON_OVERLAPS_BASES: 'Exon_overlaps_bp',
        EXON_OVERLAPS_PERCENTAGE: 'exon_overlaps_pct',
        CDS_OVERLAPS_PERCENTAGE: 'cds_overlaps_pct',
        ORIGINAL_FIELDS: 'ori_fields',
    }

def check_genome(genome):
    if genome not in SUPPORTED_GENOMES:
        sys.stdout.write('Genome ' + genome + ' is not supported. Supported genomes: ' + ', '.join(SUPPORTED_GENOMES) + '\n')
        sys.exit(1)

#################
### INTERFACE ###
#################
def get_all_features(genome, high_confidence=False, features=None, gene_names=None, only_canonical=False):
    _canon_filt = get_only_canonical_filter(genome) if only_canonical else None

    ori_genome = genome
    genome = genome.replace('GRCh37', 'hg19')
    genome = genome.replace('GRCh38', 'hg38')

    bed = _get_ensembl_file('ensembl.bed', genome)
    def _filter(x):
        if features:
            if x[BedCols.FEATURE] not in features:
                return False
        if gene_names:
            if x[BedCols.GENE] not in gene_names:
                return False
        if _canon_filt:
            if not _canon_filt(x):
                return False
        return True
    debug('Filtering BEDTool for: specific features, specific genes, canonical')
    bed = bed.filter(_filter)
    if ori_genome.startswith('GRCh'):
        def fix_chr(r):
            r.chrom = r.chrom.replace('chrM', 'MT').replace('chr', '')
            return r
        bed = bed.each(fix_chr)

    # selecting columns up to TX_OVERLAP_PERCENTAGE (to remove Hugo)
    def _select_cols(r):
        r = r[:len(BedCols.cols)-4]
        return r
    bed = bed.each(_select_cols)

    return bed

def get_merged_cds(genome):
    """
    Returns all CDS merged, used:
    - for TargQC general reports CDS coverage statistics for WGS
    - for Seq2C CNV calling when no capture BED available
    """
    bed = get_all_features(genome)
    debug('Filtering BEDTool for high confidence CDS and stop codons')
    return bed\
        .filter(lambda r: r.fields[BedCols.FEATURE] in ['CDS', 'stop_codon'])\
        .filter(high_confidence_filter)\
        .merge()

###############
### ENSEMBL ###
###############
def ensembl_gtf_fpath(genome):
    return _get_ensembl_file(join('gtf', 'ref-transcripts.gtf'), genome.split('-')[0])  # no -alt

def biomart_fpath(genome='hg38'):
    """ bm_fpath downloaded from http://www.ensembl.org/biomart

        ---------------------------------------------------------
        hg38:
        - go to http://grch37.ensembl.org/biomart
        - select "Ensembl Gene"
        - select "Human genes (GRCh37.p13)"
        - click "Attributes":
          - GENE:
            - Transcript ID
            - Transcript Support Level (TSL)
            - Associated Gene Name
            - % GC content
            - Gene type
            - Transcript type
          - EXTERNAL:
            - HGNC symbol
            - RefSeq mRNA
            - RefSeq ncRNA

        link: http://www.ensembl.org/biomart/martview/1a58c2026b9aafb613b19630264d4a54?VIRTUALSCHEMANAME=default
        &ATTRIBUTES=hsapiens_gene_ensembl.default.feature_page.ensembl_transcript_id|hsapiens_gene_ensembl.default
        .feature_page.gene_biotype|hsapiens_gene_ensembl.default.feature_page.transcript_biotype
        |hsapiens_gene_ensembl.default.feature_page.transcript_tsl|hsapiens_gene_ensembl.default.feature_page
        .percentage_gc_content|hsapiens_gene_ensembl.default.feature_page.hgnc_symbol|hsapiens_gene_ensembl.default
        .feature_page.refseq_mrna|hsapiens_gene_ensembl.default.feature_page.refseq_ncrna|hsapiens_gene_ensembl
        .default.feature_page.external_gene_name&FILTERS=&VISIBLEPANEL=attributepanel
        
        ---------------------------------------------------------
        hg19:
        - go to http://www.ensembl.org/biomart
        - select "Ensembl Genes 87"
        - select "Human genes (GRCh38.p7)"
        - click "Attributes":
          - GENE:
            - Transcript ID
            - Associated Gene Name
            - % GC content
            - Gene type
            - Transcript type
          - EXTERNAL:
            - HGNC symbol
            - RefSeq mRNA
            - RefSeq ncRNA

        link: http://grch37.ensembl.org/biomart/martview/2b820216c72bef00db4384dfc0e874fd?VIRTUALSCHEMANAME=default
        &ATTRIBUTES=hsapiens_gene_ensembl.default.feature_page.ensembl_transcript_id|hsapiens_gene_ensembl.default
        .feature_page.external_gene_name|hsapiens_gene_ensembl.default.feature_page.percentage_gc_content
        |hsapiens_gene_ensembl.default.feature_page.gene_biotype|hsapiens_gene_ensembl.default.feature_page
        .transcript_biotype|hsapiens_gene_ensembl.default.feature_page.refseq_mrna|hsapiens_gene_ensembl.default
        .feature_page.refseq_ncrna|hsapiens_gene_ensembl.default.feature_page.hgnc_symbol&FILTERS=&VISIBLEPANEL
        =attributepanel
    """
    return _get_ensembl_file('mart_export.txt', genome)

def _get_ensembl_file(fname, genome=None):
    if genome:
        return _get(join(genome.split('-')[0], fname), genome)
    else:
        return _get(join(fname))


###################
### TRANSCRIPTS ###
###################
def get_canonical_transcripts_ids(genome):
    short_genome = genome.split('-')[0]
    if short_genome.startswith('GRCh37'):
        short_genome = 'hg19'
    if short_genome.startswith('GRCh38'):
        short_genome = 'hg38'
    check_genome(short_genome)

    canon_fpath = _get(join('{genome}', 'canon_transcripts_{genome}_ensembl.txt'), genome)
    replacement_fpath = _get('canon_cancer_replacement.txt')

    canon_fpath = verify_file(canon_fpath, description='Canonical transcripts path')
    replacement_fpath = verify_file(replacement_fpath, description='Canonical cancer transcripts replacement path')

    if not canon_fpath:
        return None
    with open(canon_fpath) as f:
        canon_tx_by_gname = dict(l.strip('\n').split('\t') for l in f)
    if replacement_fpath:
        with open(replacement_fpath) as f:
            for gname, tx_id in (l.strip('\n').split('\t') for l in f):
                canon_tx_by_gname[gname] = tx_id

    return canon_tx_by_gname


def _get(relative_path, genome=None):
    """
    :param relative_path: relative path of the file inside the repository
    :param genome: genome name. Can contain chromosome name after comma, like hg19-chr20,
                   in case of BED, the returning BedTool will be with added filter.
    :return: BedTools object if it's a BED file, or filepath
    """
    chrom = None
    if genome:
        if '-chr' in genome:
            genome, chrom = genome.split('-')
        check_genome(genome)
        relative_path = relative_path.format(genome=genome)

    path = abspath(join(dirname(__file__), relative_path))
    if not isfile(path) and isfile(path + '.gz'):
        path += '.gz'

    if path.endswith('.bed') or path.endswith('.bed.gz'):
        if path.endswith('.bed.gz'):
            bedtools = which('bedtools')
            if not bedtools:
                critical('bedtools not found in PATH: ' + str(os.environ['PATH']))
            debug('BED is compressed, creating BedTool')
            bed = BedTool(path)
        else:
            debug('BED is uncompressed, creating BedTool')
            bed = BedTool(path)

        if chrom:
            debug('Filtering BEDTool for chrom ' + chrom)
            bed = bed.filter(lambda r: r.chrom == chrom)
        return bed
    else:
        return path

def get_hgnc_gene_synonyms():
    return _get('HGNC_gene_synonyms.txt')

def high_confidence_filter(x):
    return x[BedCols.TSL] in ['1', '2', 'NA', '.', None]

def get_only_canonical_filter(genome):
    canon_tx_by_gname = get_canonical_transcripts_ids(genome)
    return lambda x: x[BedCols.ENSEMBL_ID] == canon_tx_by_gname.get(x[BedCols.GENE])

def protein_coding_filter(x):
    return x[BedCols.BIOTYPE] == 'protein_coding'
