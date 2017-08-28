import uuid
from itertools import combinations
from collections import defaultdict
from functools import reduce
from copy import deepcopy

from cyvcf2 import VCF
import numpy as np
import scipy.cluster.hierarchy as hcl
import scipy.cluster
from Levenshtein import hamming

from avedata.sequence import get_sequence


def scipyclust2json(clusters, labels):
    T = scipy.cluster.hierarchy.to_tree(clusters, rd=False)

    # Create dictionary for labeling nodes by their IDs
    id2name = dict(zip(range(len(labels)), labels))

    # Create a nested dictionary from the ClusterNode's returned by SciPy
    def add_node(node, parent):
        # First create the new node and append it to its parent's children
        new_node = dict(node_id=node.id, children=[])
        parent["children"].append(new_node)

        # Recursively add the current node's children
        if node.left:
            add_node(node.left, new_node)
        if node.right:
            add_node(node.right, new_node)

    # Initialize nested dictionary for d3, then recursively iterate through tree
    d3_dendro = dict(children=[])
    add_node(T, d3_dendro)

    ordered_haplotype_ids = []

    # Label each node with the names of each leaf in its subtree
    def label_tree(n):
        # If the node is a leaf, then we have its name
        if len(n["children"]) == 0:
            n['haplotype_id'] = id2name[n["node_id"]]
            ordered_haplotype_ids.append(n['haplotype_id'])
            del n['children']
            leaf_names = [id2name[n["node_id"]]]

        # If not, flatten all the leaves in the node's subtree
        else:
            leaf_names = reduce(lambda ls, c: ls +
                                label_tree(c), n["children"], [])

        # Delete the node id since we don't need it anymore and
        # it makes for cleaner JSON
        del n["node_id"]

        return leaf_names

    label_tree(d3_dendro["children"][0])
    return d3_dendro, ordered_haplotype_ids


def get_accessions_list(filename):
    variants = VCF(filename)
    return variants.samples


class AccessionsLookupError(LookupError):
    def __init__(self, accessions):
        super().__init__()
        self.accessions = accessions


def get_variants(variant_file, chrom_id, start_position, end_position, accessions):
    region = '{0}:{1}-{2}'.format(chrom_id, start_position, end_position)
    vcf = VCF(variant_file)
    vcf_variants = vcf(region)
    all_accessions = vcf.samples
    if len(accessions) == 0:
        accessions = all_accessions

    if not set(accessions).issubset(set(all_accessions)):
        raise AccessionsLookupError(
            set(accessions).difference(set(all_accessions)))

    # sequences in a dictionary
    # with accession names as keys
    sequences = defaultdict(str)
    # positions of the variants in a dictionary
    # fetch the genotypes in the variation positions
    # store all the variant objects in an array
    variants = []
    for v in vcf_variants:
        if v.is_snp:
            variant = {
                'chrom': v.CHROM,
                'pos': v.POS,
                'id': v.ID,
                'ref': v.REF,
                'alt': v.ALT,
                'qual': v.QUAL,
                'filter': v.FILTER,
                'info': dict(v.INFO),
                'genotypes': []
            }
            for idx, (acc, genotype) in enumerate(zip(all_accessions, v.genotypes)):
                if acc not in accessions:
                    continue
                if genotype[0] == -1:
                    sequences[acc] += v.REF
                else:
                    # ignores heterozygosity
                    # always picks most frequent ALT
                    sequences[acc] += v.ALT[0]
                    # add info to variant object
                    # genotype should contain all format fields for each
                    # actual varint at this position
                    genotype = {
                        'accession': acc,
                        'genotype': str(genotype[:2])
                    }
                    for f in v.FORMAT[1:]:
                        genotype[f] = str(v.format(f)[idx])
                    variant['genotypes'].append(genotype)
            variants.append(variant)
    return variants, sequences


def cluster_sequences(sequences):
    clusters = {}
    for (accession, sequence) in sequences.items():
        if sequence in clusters:
            clusters[sequence]['accessions'].append(accession)
        else:
            clusters[sequence] = {
                'accessions': [accession],
                'haplotype_id': uuid.uuid4().hex
            }
    return list(clusters.values())


def add_variants2haplotypes(haplotypes, variants):
    # add variant information to haplotypes
    # variants should only contain genotype information
    # about genotypes present in particular haplotype
    for haplotype in haplotypes:
        haplotype['variants'] = []
        for v in variants:
            genotypes = []
            for g in v['genotypes']:
                if g['accession'] in haplotype['accessions']:
                    genotypes.append(g)
            if len(genotypes):
                haplotype_variant = deepcopy(v)
                haplotype_variant['genotypes'] = genotypes
                haplotype['variants'].append(haplotype_variant)


def add_sequence2haplotypes(haplotypes, ref_seq, start_position):
    # reconstruct the sequence based on reference and variant information of the
    # haplotype; both the sequence (a python string) and the variants from vcf
    # are indexed from zero
    for h in haplotypes:
        haplotype_sequence = list(ref_seq)
        for v in h['variants']:
            # start_position is 1-based and vcf, while seq is 0-based, require -1
            haplotype_sequence[v['pos'] - start_position - 1] = v['alt'][0]
        h['sequence'] = "".join(haplotype_sequence)


def cluster_haplotypes(haplotypes):
    # get distances between the haplotypes based on the distances between
    # the accessions
    haplotype_ids = [h['haplotype_id'] for h in haplotypes]
    haplotype_distances = []

    # if there is just one haplotype, due to for example no variants in region, then hierarchy will be a single node
    if len(haplotypes) == 1:
        root_node = {
            'haplotype_id': haplotypes[0]['haplotype_id']
        }
        return root_node, haplotypes

    # compute distances between haplotypes
    for h1, h2 in combinations(haplotypes, 2):
        seq1 = h1['sequence']
        seq2 = h2['sequence']
        # TODO check if computing distance between haplotype sequence is slower/worse
        # than using the variant of the first accession of each haplotype
        dist = hamming(seq1, seq2)
        haplotype_distances.append(dist)

    clusters = hcl.linkage(np.array(haplotype_distances))
    root_node, ordered_haplotype_ids = scipyclust2json(clusters, haplotype_ids)

    # the haplotypes and hierarchy are rendered in separate panels next to each other
    # so the first leaf in the hierarchy should be the same as the first haplotype in the list
    ordered_haplotypes = []
    for haplotype_id in ordered_haplotype_ids:
        haplotype = [h for h in haplotypes if h['haplotype_id']
                     == haplotype_id][0]
        ordered_haplotypes.append(haplotype)

    return root_node, ordered_haplotypes


def get_haplotypes(variant_file, ref_file, chrom_id, start_position, end_position, accessions):
    (variants, sequences) = get_variants(variant_file, chrom_id, start_position, end_position,
                                         accessions)

    haplotypes = cluster_sequences(sequences)
    add_variants2haplotypes(haplotypes, variants)

    # load reference sequence from a 2bit file
    ref_seq = get_sequence(ref_file, chrom_id, start_position, end_position)

    if len(haplotypes) == 0:
        return no_variants_response(accessions, ref_seq)

    add_sequence2haplotypes(haplotypes, ref_seq, start_position)

    (hierarchy, ordered_haplotypes) = cluster_haplotypes(haplotypes)

    return {
        'hierarchy': hierarchy,
        'haplotypes': ordered_haplotypes
    }


def no_variants_response(accessions, ref_seq):
    haplotype = {
        'accessions': accessions,
        'haplotype_id': uuid.uuid4().hex,
        'sequence': ref_seq,
        'variants': []
    }
    return {
        'hierarchy': {
            'haplotype_id': haplotype['haplotype_id']
        },
        'haplotypes': [haplotype]
    }
