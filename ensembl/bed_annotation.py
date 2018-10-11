#!/usr/bin/env python

import ensembl as ebl
import os
import pybedtools
import tempfile
from collections import defaultdict, OrderedDict
from contextlib import contextmanager
from os.path import isfile, join, basename
from pybedtools import BedTool
from targqc.utilz.bed_utils import verify_bed, SortableByChrom, count_bed_cols, sort_bed, clean_bed
from targqc.utilz import reference_data
from targqc.utilz.file_utils import file_transaction, adjust_path, safe_mkdir, verify_file, tx_tmpdir
from targqc.utilz.logger import critical, info
from targqc.utilz.logger import debug
from targqc.utilz.utils import OrderedDefaultDict


def bed_chrom_order(bed_fpath):
    chroms = []
    chroms_set = set()
    with open(bed_fpath) as f:
        for l in f:
            l = l.strip()
            if l and not l.startswith('#'):
                chrom = l.split('\t')[0]
                if chrom not in chroms_set:
                    chroms_set.add(chrom)
                    chroms.append(chrom)
    chr_order = {c: i for i, c in enumerate(chroms)}
    return chr_order


def get_sort_key(chr_order):
    return lambda fs: (
        chr_order[fs[ebl.BedCols.CHROM]],
        int(fs[ebl.BedCols.START]),
        int(fs[ebl.BedCols.END]),
        0 if fs[ebl.BedCols.FEATURE] == 'transcript' else 1,
        fs[ebl.BedCols.ENSEMBL_ID],
        fs[ebl.BedCols.GENE]
    )


def overlap_with_features(input_bed_fpath, output_fpath, work_dir, genome=None, **kwargs):
    return annotate(input_bed_fpath, output_fpath, work_dir, genome=genome, **kwargs)


canon_tx_by_gname = dict()


def annotate(input_bed_fpath, output_fpath, work_dir, genome=None,
             reannotate=True, high_confidence=False, only_canonical=False,
             coding_only=False, short=False, extended=False, is_debug=False, **kwargs):

    debug('Getting features from storage')
    features_bed = ebl.get_all_features(genome)
    if features_bed is None:
        critical('Genome ' + genome + ' is not supported. Supported: ' + ', '.join(ebl.SUPPORTED_GENOMES))

    if genome:
        fai_fpath = reference_data.get_fai(genome)
        chr_order = reference_data.get_chrom_order(genome)
    else:
        fai_fpath = None
        chr_order = bed_chrom_order(input_bed_fpath)

    input_bed_fpath = sort_bed(input_bed_fpath, work_dir=work_dir, chr_order=chr_order, genome=genome)

    ori_bed = BedTool(input_bed_fpath)
    ori_col_num = ori_bed.field_count()
    reannotate = reannotate or ori_col_num == 3
    pybedtools.set_tempdir(safe_mkdir(join(work_dir, 'bedtools')))
    ori_bed = BedTool(input_bed_fpath)
        # if reannotate:
        #     bed = BedTool(input_bed_fpath).cut([0, 1, 2])
        #     keep_gene_column = False
        # else:
        #     if col_num > 4:
        #         bed = BedTool(input_bed_fpath).cut([0, 1, 2, 3])
        #     keep_gene_column = True

    # features_bed = features_bed.saveas()
    # cols = features_bed.field_count()
    # if cols < 12:
    #     features_bed = features_bed.each(lambda f: f + ['.']*(12-cols))
    if high_confidence:
        features_bed = features_bed.filter(ebl.high_confidence_filter)
    if only_canonical:
        features_bed = features_bed.filter(ebl.get_only_canonical_filter(genome))
    if coding_only:
        features_bed = features_bed.filter(ebl.protein_coding_filter)
    # unique_tx_by_gene = find_best_tx_by_gene(features_bed)

    info('Extracting features from Ensembl GTF')
    features_bed = features_bed.filter(lambda x:
        x[ebl.BedCols.FEATURE] in ['exon', 'CDS', 'stop_codon', 'transcript'])
        # x[ebl.BedCols.ENSEMBL_ID] == unique_tx_by_gene[x[ebl.BedCols.GENE]])

    info('Overlapping regions with Ensembl data')
    if is_debug:
        ori_bed = ori_bed.saveas(join(work_dir, 'bed.bed'))
        debug(f'Saved regions to {ori_bed.fn}')
        features_bed = features_bed.saveas(join(work_dir, 'features.bed'))
        debug(f'Saved features to {features_bed.fn}')
    annotated = _annotate(ori_bed, features_bed, chr_order, fai_fpath, work_dir, ori_col_num,
                          high_confidence=False, reannotate=reannotate, is_debug=is_debug, **kwargs)

    full_header = [ebl.BedCols.names[i] for i in ebl.BedCols.cols]
    add_ori_extra_fields = ori_col_num > 3
    if not reannotate and ori_col_num == 4:
        add_ori_extra_fields = False  # no need to report the original gene field if we are not re-annotating

    info('Saving annotated regions...')
    total = 0
    with file_transaction(work_dir, output_fpath) as tx:
        with open(tx, 'w') as out:
            header = full_header[:6]
            if short:
                header = full_header[:4]
            if extended:
                header = full_header[:-1]
            if add_ori_extra_fields:
                header.append(full_header[-1])

            if extended:
                out.write('## ' + ebl.BedCols.names[ebl.BedCols.TX_OVERLAP_PERCENTAGE] +
                          ': part of region overlapping with transcripts\n')
                out.write('## ' + ebl.BedCols.names[ebl.BedCols.EXON_OVERLAPS_PERCENTAGE] +
                          ': part of region overlapping with exons\n')
                out.write('## ' + ebl.BedCols.names[ebl.BedCols.CDS_OVERLAPS_PERCENTAGE] +
                          ': part of region overlapping with protein coding regions\n')
                out.write('\t'.join(header) + '\n')
            for full_fields in annotated:
                fields = full_fields[:6]
                if short:
                    fields = full_fields[:4]
                if extended:
                    fields = full_fields[:-1]
                if add_ori_extra_fields:
                    fields.append(full_fields[-1])

                out.write('\t'.join(map(_format_field, fields)) + '\n')
                total += 1
    
    debug('Saved ' + str(total) + ' total annotated regions')
    return output_fpath


class Region(SortableByChrom):
    def __init__(self, chrom, start, end, ref_chrom_order, gene_symbol=None, exon=None,
                 strand=None, other_fields=None):
        SortableByChrom.__init__(self, chrom, ref_chrom_order)
        self.chrom = chrom
        self.start = start
        self.end = end
        self.symbol = gene_symbol
        self.exon = exon
        self.strand = strand
        self.other_fields = other_fields or []
        self.total_merged = 0

    def __str__(self):
        fs = [
            self.chrom,
            self.start,
            self.end,
            self.symbol,
            self.exon,
            self.strand
        ] + self.other_fields
        fs = map(_format_field, fs)
        return '\t'.join(fs) + '\n'

    def get_key(self):
        return SortableByChrom.get_key(self), self.start, self.end, self.symbol

    def header(self):
        pass


def _format_field(value):
    if isinstance(value, list) or isinstance(value, set):
        return ', '.join(_format_field(v) for v in value)
    elif isinstance(value, float):
        return '{:.1f}'.format(value)
    else:
        return str(value) if value is not None else '.'


# def find_best_tx_by_gene(features_bed):
#     all_transcripts = features_bed.filter(lambda x: x[ebl.BedCols.FEATURE] in ['transcript'])
#
#     tx_by_gene = defaultdict(list)
#     for tx_region in all_transcripts:
#         tx_by_gene[tx_region.name].append(tx_region)
#     unique_tx_by_gene = dict()
#     for g, txs in tx_by_gene.items():
#         tsl1_txs = [tx for tx in txs if tx[ebl.BedCols.TSL] in ['1', 'NA']]
#         if tsl1_txs:
#             txs = tsl1_txs[:]
#         best_tx = max(txs, key=lambda tx_: int(tx_[ebl.BedCols.END]) - int(tx_[ebl.BedCols.START]))
#         unique_tx_by_gene[g] = best_tx[ebl.BedCols.ENSEMBL_ID]
#     return unique_tx_by_gene


def tx_priority_sort_key(x):
    # overlaps_cds_key = 0
    # ind = x[ebl.BedCols.CDS_OVERLAPS_PERCENTAGE]
    # if len(x) > ind and x[ind] and x[ind] > 0:
    #     overlaps_cds_key = 1

    overlap_key = tuple([(-x[ind] if len(x) > ind and x[ind] is not None else 0)
       for ind in [ebl.BedCols.TX_OVERLAP_PERCENTAGE,
                   ebl.BedCols.CDS_OVERLAPS_PERCENTAGE,
                   ebl.BedCols.EXON_OVERLAPS_PERCENTAGE]])
    
    biotype_rank = ['protein_coding', 'rna', 'decay', 'sense_', 'antisense', '__default__', 'translated_', 'transcribed_']
    biotype = '__default__'
    for bt in biotype_rank:
        if bt in x[ebl.BedCols.BIOTYPE].lower():
            biotype = bt
            break
    biotype_key = biotype_rank.index(biotype)

    tsl_key = {'1': 0, '2': 2, '3': 3, '4': 4, '5': 5}.get(x[ebl.BedCols.TSL], 1)

    is_canon = x[ebl.BedCols.ENSEMBL_ID] == canon_tx_by_gname.get(x[ebl.BedCols.GENE])
    canon_tx_key = 0 if is_canon else 1
    
    length_key = -(int(x[ebl.BedCols.END]) - int(x[ebl.BedCols.START]))

    return overlap_key, biotype_key, tsl_key, canon_tx_key, length_key


# def select_best_tx(overlaps_by_tx):
#     tx_overlaps = [(o, size) for os in overlaps_by_tx.values() for (o, size) in os if o[ebl.BedCols.FEATURE] == 'transcript']
#     tsl1_overlaps = [(o, size) for (o, size) in tx_overlaps if o[ebl.BedCols.TSL] in ['1', 'NA']]
#     if tsl1_overlaps:
#         tx_overlaps = tsl1_overlaps
#     (best_overlap, size) = max(sorted(tx_overlaps, key=lambda (o, size): int(o[ebl.BedCols.END]) - int(o[ebl.BedCols.START])))
#     tx_id = best_overlap[ebl.BedCols.ENSEMBL_ID]
#     return tx_id


def _resolve_ambiguities(overlaps_by_tx_by_gene_by_loc, chrom_order,
         collapse_exons=True, output_features=False, ambiguities_method='best_one'):
    # sort transcripts by "quality" and then select which tx id to report in case of several overlaps
    # (will be useful further when interating exons)
    # annotated_by_tx_by_gene_by_loc = OrderedDict([
    #     (loc, OrderedDict([
    #         (gname, sorted(annotated_by_tx.values(), key=tx_sort_key))
    #             for gname, annotated_by_tx in annotated_by_tx_by_gene.items()
    #     ])) for loc, annotated_by_tx_by_gene in annotated_by_tx_by_gene_by_loc.items()
    # ])

    annotated = []
    for (chrom, start, end, a_extra_columns), overlaps_by_tx_by_gene in overlaps_by_tx_by_gene_by_loc.items():
        features = dict()
        annotation_alternatives = []
        for gname, overlaps_by_tx in overlaps_by_tx_by_gene.items():
            consensus = [None for _ in ebl.BedCols.cols]
            consensus[:3] = chrom, start, end
            consensus[ebl.BedCols.ORIGINAL_FIELDS] = '|'.join(a_extra_columns)
            if gname:
                consensus[3] = gname
            consensus[ebl.BedCols.FEATURE] = 'capture'
            # consensus[ebl.BedCols.EXON_OVERLAPS_BASES] = 0
            consensus[ebl.BedCols.TX_OVERLAP_PERCENTAGE] = 0
            consensus[ebl.BedCols.EXON_OVERLAPS_PERCENTAGE] = 0
            consensus[ebl.BedCols.CDS_OVERLAPS_PERCENTAGE] = 0
            consensus[ebl.BedCols.EXON] = set()

            if not overlaps_by_tx:  # not annotated or already annotated but gene did not match the intersection
                annotated.append(consensus)
                continue

            all_tx = []
            for xx in overlaps_by_tx.values():
                for x, overlap_bp in xx:
                    if x[ebl.BedCols.FEATURE] == 'transcript':
                        x[ebl.BedCols.TX_OVERLAP_PERCENTAGE] = 100.0 * overlap_bp / (int(end) - int(start))
                        all_tx.append(x)

            # if not all_tx:
            #     annotated.append(consensus)
            #     continue
            # tx_by_key = {tx_priority_sort_key(tx): tx for tx in all_tx}
            # overlaps = []
            # for x in tx_by_key.values():
            #     overlaps.extend(overlaps_by_tx[x[ebl.BedCols.ENSEMBL_ID]])

            tx_sorted_list = [x[ebl.BedCols.ENSEMBL_ID] for x in sorted(all_tx, key=tx_priority_sort_key)]
            if not tx_sorted_list:
                annotated.append(consensus)
                continue
            tx_id = tx_sorted_list[0]
            overlaps = overlaps_by_tx[tx_id]
            
            for fields, overlap_size in overlaps:
                c_overlap_bp = overlap_size
                c_overlap_pct = 100.0 * c_overlap_bp / (int(end) - int(start))

                if output_features:
                    f_start = int(fields[1])
                    f_end = int(fields[2])
                    feature = features.get((f_start, f_end))
                    if feature is None:
                        feature = [None for _ in ebl.BedCols.cols]
                        feature[:len(fields)] = fields
                        # feature[ebl.BedCols.TX_OVERLAP_BASES] = 0
                        # feature[ebl.BedCols.TX_OVERLAP_PERCENTAGE] = 0
                        features[(f_start, f_end)] = feature
                    # feature[ebl.BedCols.TX_OVERLAP_BASES] += c_overlap_bp
                    # feature[ebl.BedCols.TX_OVERLAP_PERCENTAGE] += 100.0 * c_overlap_bp / (int(f_end) - int(f_start))
                    # TODO: don't forget to merge BED if not

                if fields[ebl.BedCols.FEATURE] == 'transcript':
                    consensus[ebl.BedCols.GENE] = gname
                    consensus[ebl.BedCols.STRAND] = fields[ebl.BedCols.STRAND]
                    consensus[ebl.BedCols.BIOTYPE] = fields[ebl.BedCols.BIOTYPE]
                    consensus[ebl.BedCols.ENSEMBL_ID] = fields[ebl.BedCols.ENSEMBL_ID]
                    consensus[ebl.BedCols.TSL] = fields[ebl.BedCols.TSL]
                    # consensus[ebl.BedCols.TX_OVERLAP_BASES] = c_overlap_bp
                    consensus[ebl.BedCols.TX_OVERLAP_PERCENTAGE] = c_overlap_pct

                elif fields[ebl.BedCols.FEATURE] == 'exon':
                    consensus[ebl.BedCols.EXON_OVERLAPS_PERCENTAGE] += c_overlap_pct
                    consensus[ebl.BedCols.EXON].add(int(fields[ebl.BedCols.EXON]))

                elif fields[ebl.BedCols.FEATURE] == 'CDS':
                    consensus[ebl.BedCols.CDS_OVERLAPS_PERCENTAGE] += c_overlap_pct

            consensus[ebl.BedCols.EXON] = sorted(list(consensus[ebl.BedCols.EXON]))

            # annotated.append(consensus)
            annotation_alternatives.append(consensus)

        if len(annotation_alternatives) > 1:  # unless asked for all, selecting the top best annotation
            annotation_alternatives.sort(key=tx_priority_sort_key)
            # annotation_alternatives = [a for a in annotation_alternatives if a[ebl.BedCols.CDS_OVERLAPS_PERCENTAGE] > 50]
            # choices=['best_one', 'best_ask', 'best_all', 'all_ask', 'all'],
            if 'best_' in ambiguities_method:
                best_alt = annotation_alternatives[0]
                if ambiguities_method == 'best_one':
                    annotation_alternatives = [best_alt]
                else:
                    annotation_alternatives = [a for a in annotation_alternatives if tx_priority_sort_key(a) == tx_priority_sort_key(best_alt)]
            if len(annotation_alternatives) > 1 and ambiguities_method in '_ask':
                choice_indices = input('Please choose alternative (sorted by confidence):\n' +
                    ''.join([str(i) + ': ' + '\t'.join(str(f) for f in a) + '\n' for i, a in enumerate(annotation_alternatives)]))
                choice_indices = choice_indices.split(',')
                annotation_alternatives = [annotation_alternatives[int(i)] for i in choice_indices]
                
        annotated.extend(annotation_alternatives)

        features = sorted(features.values(), key=get_sort_key(chrom_order))
        if output_features:
            annotated.extend(features)
    return annotated


def _annotate(bed, ref_bed, chr_order, fai_fpath, work_dir, ori_col_num,
              high_confidence=False, reannotate=False, is_debug=False, **kwargs):
    # if genome:
        # genome_fpath = cut(fai_fpath, 2, output_fpath=intermediate_fname(work_dir, fai_fpath, 'cut2'))
        # intersection = bed.intersect(ref_bed, sorted=True, wao=True, g='<(cut -f1,2 ' + fai_fpath + ')')
        # intersection = bed.intersect(ref_bed, sorted=True, wao=True, genome=genome.split('-')[0])
    # else:

    intersection_bed = None
    intersection_fpath = None
    
    pybedtools.set_tempdir(safe_mkdir(join(work_dir, 'bedtools')))
    if is_debug:
        intersection_fpath = join(work_dir, 'intersection.bed')
        if isfile(intersection_fpath):
            info('Loading from ' + intersection_fpath)
            intersection_bed = BedTool(intersection_fpath)
    if not intersection_bed:
        if count_bed_cols(fai_fpath) == 2:
            debug('Fai fields size is 2 ' + fai_fpath)
            intersection_bed = bed.intersect(ref_bed, wao=True, g=fai_fpath)
        else:
            debug('Fai fields is ' + str(count_bed_cols(fai_fpath)) + ', not 2')
            intersection_bed = bed.intersect(ref_bed, wao=True)
    if is_debug and not isfile(intersection_fpath):
        intersection_bed.saveas(intersection_fpath)
        debug('Saved intersection to ' + intersection_fpath)

    total_annotated = 0
    total_uniq_annotated = 0
    total_off_target = 0

    met = set()

    overlaps_by_tx_by_gene_by_loc = OrderedDefaultDict(lambda: OrderedDefaultDict(lambda: defaultdict(list)))
    # off_targets = list()

    expected_fields_num = ori_col_num + len(ebl.BedCols.cols[:-4]) + 1
    for i, intersection_fields in enumerate(intersection_bed):
        inters_fields_list = list(intersection_fields)
        if len(inters_fields_list) < expected_fields_num:
            critical(f'Cannot parse the reference BED file - unexpected number of lines '
                     '({len(inters_fields_list} in {inters_fields_list}' +
                     ' (less than {expected_fields_num})')

        a_chr, a_start, a_end = intersection_fields[:3]
        a_extra_columns = intersection_fields[3:ori_col_num]

        overlap_fields = [None for _ in ebl.BedCols.cols]

        overlap_fields[:len(intersection_fields[ori_col_num:-1])] = intersection_fields[ori_col_num:-1]
        keep_gene_column = not reannotate
        a_gene = None
        if keep_gene_column:
            a_gene = a_extra_columns[0]

        e_chr = overlap_fields[0]
        overlap_size = int(intersection_fields[-1])
        assert e_chr == '.' or a_chr == e_chr, f'Error on line {i}: chromosomes don\'t match ({a_chr} vs {e_chr}). Line: {intersection_fields}'

        # fs = [None for _ in ebl.BedCols.cols]
        # fs[:3] = [a_chr, a_start, a_end]
        reg = (a_chr, int(a_start), int(a_end), tuple(a_extra_columns))

        if e_chr == '.':
            total_off_target += 1
            # off_targets.append(fs)
            overlaps_by_tx_by_gene_by_loc[reg][a_gene] = OrderedDefaultDict(list)

        else:
            # fs[3:-1] = db_feature_fields[3:-1]
            total_annotated += 1
            if (a_chr, a_start, a_end) not in met:
                total_uniq_annotated += 1
                met.add((a_chr, a_start, a_end))

            e_gene = overlap_fields[ebl.BedCols.GENE]
            if keep_gene_column and e_gene != a_gene:
                overlaps_by_tx_by_gene_by_loc[reg][a_gene] = OrderedDefaultDict(list)
            else:
                transcript_id = overlap_fields[ebl.BedCols.ENSEMBL_ID]
                overlaps_by_tx_by_gene_by_loc[reg][e_gene][transcript_id].append((overlap_fields, overlap_size))

    info('  Total annotated regions: ' + str(total_annotated))
    info('  Total unique annotated regions: ' + str(total_uniq_annotated))
    info('  Total off target regions: ' + str(total_off_target))
    info('Resolving ambiguities...')
    annotated = _resolve_ambiguities(overlaps_by_tx_by_gene_by_loc, chr_order, **kwargs)

    return annotated


def _save_regions(regions, fpath):
    with open(fpath, 'w') as off_target_f:
        for r in regions:
            off_target_f.write(str(r))

    return fpath


def _split_reference_by_priority(cnf, features_bed_fpath):
    features = ['CDS', 'Exon', 'Transcript', 'Gene']
    info('Splitting the reference file into ' + ', '.join(features))
    features_and_beds = []
    for f in features:
        features_and_beds.append((f, BedTool(features_bed_fpath).filter(lambda x: x[6] == f)))
    return features_and_beds
