"""
Prototype forward simulator where we simplify inline, using the Individual
structures directly.
"""
import argparse
from dataclasses import dataclass
import random
import collections
import heapq
import sys
from typing import List, Tuple, Dict, Optional

import tskit

# Notes: this is not working yet. The idea is that we should be able to
# maintain the data structure needed for simplify dynamically as (a)
# ancestry segments are added when samples are added as children of
# the individuals and (b) as individuals die. In the case of (a), we
# can have coalescences occur or not, as new children are added to a
# parent. That's easy to track, by looking at the overlapping segments
# within the children. In (b) when an individual dies, this means a
# loss of ancestral material up the trees. Because we're keeping the
# list of parents for each individual, we should be able to propagate
# this loss up the trees in a simplify-esque way. If we do it one-by-one,
# then the changes to the data structures should be easy enough to reason
# about.

# The goal of all this is to avoid the O(N) cost of visiting all N
# reachable individuals each generation. By keeping changes local to
# the actual losses and gains of ancestral material, we should be able
# to keep this to something more like log(N). This seems pointless if
# we're going to be iterating over all the trees each generation though.
# Perhaps we can work around this by keeping a tree for each selected
# site 'live' in memory, and update it as well as edges are added and
# lost. If we use hash tables for the trees, then presumably this isn't
# too much memory.

# Not sure if any of this will work, but that's the thinking anyway.


def make_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("-N", type=int, default=8, help="Haploid population size")
    parser.add_argument(
        "--death_probability", "-d", type=float, default=0.5, help="Death probability"
    )
    parser.add_argument(
        "--simlen",
        "-s",
        type=int,
        default=200,
        help="Simulation length (number of birth steps)",
    )
    parser.add_argument(
        "--genome_length", "-L", type=int, default=5, help="Genome length"
    )
    parser.add_argument(
        "--seed", "-S", type=int, default=501251, help="Random number seed"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Print excessive amount of stuff to the screen",
    )

    return parser


def validate_args(args):
    assert args.N > 1
    assert args.seed >= 0
    assert args.simlen > 0
    assert args.death_probability > 0.0 and args.death_probability <= 1.0
    assert args.genome_length > 1


@dataclass
class ParentNode:
    time: float
    children: List[int]

    def __post_init__(self):
        self.children = sorted(self.children)


@dataclass
class Topology:
    left: int
    right: int
    parents: Dict[int, ParentNode]


def make_topologies(ts: tskit.TreeSequence, node_labels=None):
    topologies = []

    for t in ts.trees():
        topo = {}
        for n in t.preorder():
            if node_labels is None:
                topo[n] = ParentNode(ts.node(n).time, [c for c in t.children(n)])
            else:
                # Back-label from tkit's IDs to those used in this prototype
                p = node_labels[n]
                topo[p] = ParentNode(
                    ts.node(n).time, [node_labels[c] for c in t.children(n)]
                )
        temp = Topology(int(t.interval[0]), int(t.interval[1]), topo)
        if len(topologies) > 0:
            # Deal w/the fact that we are not squashing edges.
            if temp.parents != topologies[-1].parents:
                topologies.append(temp)
            else:
                topologies[-1].right = temp.right
        else:
            topologies.append(temp)

    return topologies


def assert_non_overlapping(segments):
    for j in range(1, len(segments)):
        x = segments[j - 1]
        y = segments[j]
        assert x.right <= y.left, f"{segments[j-1]} {segments[j]}"


def overlapping_segments(segments):
    """
    Returns an iterator over the (left, right, X) tuples describing the
    distinct overlapping segments in the specified set.
    """
    S = sorted(segments, key=lambda x: x.left)
    n = len(S)
    # Insert a sentinel at the end for convenience.
    S.append(Segment(sys.float_info.max, 0, None))
    right = S[0].left
    X = []
    j = 0
    while j < n:
        # Remove any elements of X with right <= left
        left = right
        X = [x for x in X if x.right > left]
        if len(X) == 0:
            left = S[j].left
        while j < n and S[j].left == left:
            X.append(S[j])
            j += 1
        j -= 1
        right = min(x.right for x in X)
        right = min(right, S[j + 1].left)
        yield left, right, X
        j += 1

    while len(X) > 0:
        left = right
        X = [x for x in X if x.right > left]
        if len(X) > 0:
            right = min(x.right for x in X)
            yield left, right, X


def propagate_upwards(ind, verbose):
    # NOTE: using heapq here and defining __lt__
    # such that individual is sorted by time (present to past)
    # greatly cuts the workload here ON TOP OF what is
    # accomplished by not double-entering nodes into the stack
    # (see NOTE below).
    # This isn't working.
    # print("PROPAGATE", ind)
    stack = [ind]
    while len(stack) > 0:
        # ind = stack.pop()
        ind = heapq.heappop(stack)
        if verbose is True:
            print("VISIT")
        # print("\t", ind)
        # ind.print_state()
        # We're visting everything here at the moment, but we don't need to.
        # We should only have to visit the parents for which we have ancestral
        # segents, and so the areas of the graph we traverse should be
        # quickly localised.
        # print("before")
        # ind.print_state()
        # print(f"updating {ind}")
        ind.update_ancestry(verbose)
        # print("after")
        # ind.print_state()
        for parent in ind.parents:
            # NOTE: naively adding all parents to
            # the stack results in double-processing some
            # parents b/c we often process > 1 child node
            # before getting to the parent, causing a big
            # performance loss.
            if parent not in stack:
                assert parent.time < ind.time
                # stack.append(parent)
                heapq.heappush(stack, parent)


def record_inheritance(left, right, parent, child):
    """
    Record the inheritances of genetic material from parent to child over the
    from coordinate left to right.
    """
    child.parents.add(parent)
    parent.add_child_segment(child, left, right)
    # print("record", child, parent, child.parents)


class Segment(object):
    """
    An ancestral segment mapping a given individual to a half-open genomic
    interval [left, right).
    """

    def __init__(self, left, right, child):
        self.left = left
        self.right = right
        self.child = child

    def __repr__(self):
        return repr((self.left, self.right, self.child))

    def __eq__(self, o):
        return self.left == o.left and self.right == o.right and self.child == o.child


class AncestryOverlap(object):
    """
    An ancestral segment mapping a given individual to a half-open genomic
    interval [left, right).
    """

    def __init__(self, left, right, child, mapped_node):
        self.left = left
        self.right = right
        self.child = child
        self.mapped_node = mapped_node

    def __repr__(self):
        return repr((self.left, self.right, self.child, self.mapped_node))

    def __eq__(self, o):
        return (
            self.left == o.left
            and self.right == o.right
            and self.child == o.child
            and self.mapped_node == o.mapped_node
        )


@dataclass
class ChildInputDetails:
    input_number_segs: int
    output_number_segs: int


class Individual(object):
    """
    Class representing a single individual that was alive at some time.
    """

    def __init__(self, time, index, is_alive=True):
        self.time = time
        # The direct children segments of this individual. For each unique
        # child, we keep a list of the ancestral segments that it has inherited.
        self.children = collections.defaultdict(list)
        # The ancestry mapping for this individual, as derived from the children
        # segments. If this individual is currently alive, this is a single
        # segment mapping to itself. If not, each segment records the closest
        # tree node (i.e., where there is a sample or a coalescence). Coalescences
        # occur when overlaps happen in the children.
        self.ancestry = []
        # The set of parent Individuals from which this individual has inherited
        # genetic material.
        # FIXME: this comment is not quite right.  For new births, it is the
        # parents from which segments are inherited.  For older nodes, it
        # may or may not refer to parents transmitting unary edges to self.
        self.parents = set()
        self.is_alive = is_alive
        self.index = index

    def __repr__(self):
        return f"Individual(index={self.index}, time={self.time}, is_alive={self.is_alive})"

    def __hash__(self) -> int:
        return self.index

    def __lt__(self, other):
        """
        Sort by birth time present -> past
        """
        return self.time > other.time

    def print_state(self):
        print(repr(self))
        print("children = ")
        for child, intervals in self.children.items():
            print("\t", child, "->", [(x.left, x.right) for x in intervals])
        print("ancestry = ")
        for i in self.ancestry:
            print(i)
        print("parents = ", self.parents)

    def add_child_segment(self, child, left, right):
        """
        Adds the ancestry for the specified child over the specified interval.
        """
        self.children[child].append(Segment(left, right, None))

    def update_child_segments(
        self, child, left, right, details: Dict["Individual", ChildInputDetails]
    ):
        # TODO: seems like we should be able to "squash" edges on the fly here?
        if child not in details:
            details[child] = ChildInputDetails(0, 0)

        seg = Segment(left, right, None)

        if details[child].output_number_segs < details[child].input_number_segs:
            n = details[child].output_number_segs
            if n > 0:
                if self.children[child][n - 1].right == left:
                    self.children[child][n - 1].right = right
                else:
                    self.children[child][n] = seg
                    details[child].output_number_segs += 1
            else:
                self.children[child][n] = seg
                details[child].output_number_segs += 1
        else:
            n = details[child].output_number_segs
            assert n == len(self.children[child])
            if n > 0:
                if self.children[child][n - 1].right == left:
                    self.children[child][n - 1].right = right
                else:
                    self.children[child].append(seg)
                    details[child].output_number_segs += 1
            else:
                self.children[child].append(seg)
                details[child].output_number_segs += 1

    def remove_sample_mapping(self, sequence_length):
        """
        Alive ("sample") individuals (nodes) contain a segment [0, sequence_length), self.
        When such individuals die, this segment needs to be removed.
        """

        def not_a_sample_map(seg, sequence_length) -> bool:
            assert seg.child is not None
            if seg.child.index != self.index:
                return True
            if not (seg.left == 0 and seg.right == sequence_length):
                return True
            return False

        self.ancestry = [
            i
            for i in filter(
                lambda seg: not_a_sample_map(seg, sequence_length),
                self.ancestry,
            )
        ]

    def intersecting_ancestry(self):
        """
        Returns the list of intervals over which this interval's children intersect
        with those children's ancestry segments.
        """
        S = []
        for child, intervals in self.children.items():
            for e in intervals:
                for x in child.ancestry:
                    if x.right > e.left and e.right > x.left:
                        y = AncestryOverlap(
                            max(x.left, e.left), min(x.right, e.right), child, x.child
                        )
                        S.append(y)
        return S

    def update_ancestry(self, verbose):
        # TODO: squash edges
        # TODO: do only 1 pass through self.ancestry
        # TODO: figure out a return value meaning "this node's ancestry changed"
        # FIXME: overlapping generations tend to fail asserts when run for short periods of  time,
        #        but the same seeds do "just fine" in longer simulation lengths.
        S = self.intersecting_ancestry()

        if verbose is True:
            print(f"START {self.index}")
            print(f"S = {S}")
            for c in self.children.keys():
                print(f"child {c} has ancestry {c.ancestry}, children {c.children}")
            self.print_state()

        # Do not "bulk" clear: we need to know
        # if current children are no longer
        # children when done.
        # for c, s in self.children.items():
        #     s.clear()

        input_child_details = {
            c: ChildInputDetails(len(s), 0) for c, s in self.children.items()
        }

        # print("START = ", input_child_details)

        # current_ancestry_seg = 0
        # input_ancestry_len = len(self.ancestry)
        # output_ancestry = [Segment(a.left, a.right, a.child) for a in self.ancestry]
        # unaryness = []

        # for i, a in enumerate(output_ancestry):
        #     if a.child is not self:
        #         unaryness.append(i)

        # print("UNARYNESS = ", unaryness)

        # NOTE: this is a placeholder for smartly detecting ancestry changes.
        # We are forced into "deep" copies here b/c some times (but not all the time!)
        # we get burned by Python's reference model.
        input_ancestry = [Segment(a.left, a.right, a.child) for a in self.ancestry]
        # input_ancestry2 = [Segment(a.left, a.right, a.child) for a in self.ancestry]
        input_ancestry_unary_indexes = [
            i for i, j in enumerate(self.ancestry) if j.child is not self
        ]
        input_ancestry_non_unary_indexes = [
            r
            for r in reversed(
                [i for i, j in enumerate(self.ancestry) if j.child is self]
            )
        ]
        # if not self.is_alive:
        #     self.ancestry.clear()

        ancestry_change_detected = False

        for left, right, X in overlapping_segments(S):
            assert right > left
            if verbose is True:
                print("ANCESTRY info = ", left, right, X)
            if len(X) == 1:
                mapped_ind = X[0].child
                # NOTE: TODO: FIXME: WARNING:
                # I think there's a bug here:
                # If mapped_ind.is_alive, but not self.is_alive,
                # we are failing to maintain a parent/child relationship.
                # The only symptom of this (so far...) is a (rare) UNREACHABLE
                # UNARY warning if we run with -v.
                # The "trick" (perhaps one of many) is that we risk adding redundant
                # ancestry down below...
                if verbose is True:
                    print("unary", mapped_ind, X[0])
                # unary_mapped_ind.append(mapped_ind)
                if self in mapped_ind.parents:
                    # need to guard against unary
                    # edges to the right of coalescences
                    if verbose is True:
                        print("YES", mapped_ind, self in mapped_ind.parents)
                    # if mapped_ind.is_alive and self.is_alive:
                    if self.is_alive:
                        if mapped_ind.is_alive:
                            if verbose is True:
                                print("SEG ADDING", mapped_ind)
                            # self.add_child_segment(mapped_ind, left, right)
                            self.update_child_segments(
                                mapped_ind, left, right, input_child_details
                            )
                        else:
                            # NOTE: TODO: FIXME:
                            # I am not happy with this logic.
                            # We are taking a unary transmission and moving
                            # it pastwards in time.  In general, we want unary
                            # transmissions to derive from the most recent possible node,
                            # so that things like mutation simplification cannot cause
                            # "mutation time travel"
                            # A counter argument is that we get incorrect topologies out
                            # withouth this logic, so it is possible that I'm confusing myself
                            # at the moment.
                            # Aha -- this happens when a descandant node coalesced on this Segment
                            # but no longer does, which pushes the ancestry of that Segment pastwards.
                            # Aha, revisited -- this is handling the "special" case of descendants
                            # of "alive" nodes, which is similar to "sample" nodes.
                            if verbose is True:
                                print(
                                    "NEED TO PROCESS UNARY THRU DEAD UNARY NODE",
                                    mapped_ind,
                                )
                            mapped_ind = X[0].mapped_node
                            if self not in mapped_ind.parents:
                                mapped_ind.parents.add(self)
                            self.update_child_segments(
                                mapped_ind,
                                left,
                                right,
                                input_child_details,
                            )
                    elif mapped_ind.is_alive:
                        if verbose is True:
                            print(f"ALIVE UNARY {mapped_ind} descending from {self}")
            else:
                mapped_ind = self

                for x in X:
                    self.update_child_segments(
                        x.mapped_node, left, right, input_child_details
                    )
                    x.mapped_node.parents.add(self)

                if mapped_ind in self.children:
                    # NOTE: this is a really annoyting gotcha:
                    # the defaultdict(list) adds [] to self.children
                    # if it does not exist
                    assert_non_overlapping(self.children[mapped_ind])
            # If an individual is alive it always has ancestry over the
            # full segment, so we don't overwrite this.
            if not self.is_alive:
                if len(input_ancestry_non_unary_indexes) > 0:
                    idx = input_ancestry_non_unary_indexes.pop()
                    assert self.ancestry[idx].child is self
                    if (
                        left != self.ancestry[idx].left
                        or right != self.ancestry[idx].right
                        or mapped_ind is not self.ancestry[idx].child
                    ):
                        ancestry_change_detected = True
                    self.ancestry[idx] = Segment(left, right, mapped_ind)
                else:
                    ancestry_change_detected = True
                    self.ancestry.append(Segment(left, right, mapped_ind))

        if not self.is_alive:
            for i in input_ancestry_non_unary_indexes:
                assert self.ancestry[i].child is self
                ancestry_change_detected = True
                self.ancestry[i].child = None
            for i in input_ancestry_unary_indexes:
                assert self.ancestry[i].child is not self
                if not (
                    self.ancestry[i].child.is_alive
                    or (
                        len(self.ancestry[i].child.children) > 0
                        and self in self.ancestry[i].child.parents
                    )
                ):
                    ancestry_change_detected = True
                    self.ancestry[i].child = None

            xxx = any([x.child is None for x in self.ancestry])

            self.ancestry = sorted(
                [i for i in filter(lambda x: x.child is not None, self.ancestry)],
                key=lambda x: x.left,
            )

            if verbose is True and xxx is True:
                print("INPUT UNARY ANCESTRY REMOVED")

        assert_non_overlapping(self.ancestry)

        if not self.is_alive:
            for c, segments in self.children.items():
                if c is not self:
                    if len(segments) > 0:
                        assert self in c.parents, f"{self} {c} {self.ancestry}"
        to_prune = []
        for c, s in self.children.items():
            # Not the most Pythonic, but it is a true in-place edit
            del s[input_child_details[c].output_number_segs :]
            if len(s) == 0:
                # there are no coalescences from parent -> child
                if self in c.parents:  # and not self.is_alive:
                    if verbose is True:
                        print(
                            f"REMOVING {self} from parents of {c}, {c.is_alive}, {c.children}"
                        )
                    c.parents.remove(self)
                to_prune.append(c)
        for p in to_prune:
            self.children.pop(p, None)
        for a in self.ancestry:
            if a.child in self.children and a.child is not self:
                assert self in a.child.parents, f"{self} {a} {self.ancestry}"
        if verbose is True:
            print("DONE")
            self.print_state()
        ancestry_changed = input_ancestry != self.ancestry
        assert ancestry_change_detected == ancestry_changed
        if verbose is True:
            if ancestry_changed:
                print("ANCESTRY HAS CHANGED")
            else:
                print("ANCESTRY HAS NOT CHANGED")
                print(input_ancestry)
                print(self.ancestry)
            print("OUT")

        assert self not in self.parents
        # Something CLOSE TO, but not EXACTLY, like the
        # next line gets on on the path to not visiting
        # the entire graph.  If we visit parents
        # if this is true or ind.is_alive, we'll get
        # the same trees out for small sims but then get
        # assertions on larger sims.
        # NOTE: we also need to account for a concept of "all ancestry
        # segments are unary" in the return value.
        # NOTE: need to look into "absorbed any unary" idea into the return value.
        # rv = ancestry_changed or children_changed or len(self.ancestry) == 0


@dataclass
class TransmissionInfo:
    """
    Used for export to tskit
    """

    parent: Individual
    child: Individual
    left: int
    right: int


class Simulator(object):
    """
    Simple Wright-Fisher simulator using standard periodic simplify.
    """

    def __init__(self, population_size, sequence_length, death_proba=1.0, seed=None):
        self.population_size = population_size
        self.sequence_length = sequence_length
        self.death_proba = death_proba
        self.rng = random.Random(seed)
        self.time = 1
        self.population = [Individual(self.time, i) for i in range(population_size)]
        self.next_individual_index = population_size
        self.transmissions: List[TransmissionInfo] = []
        # self.leftover_alive = []

        # Everyone starts out alive, so has to map to self
        # (Overlapping generations fails fast if this isn't done)
        for i in self.population:
            assert len(i.ancestry) == 0
            assert i.index < population_size
            i.ancestry.append(Segment(0, sequence_length, i))

    def run_generation(self, verbose):
        """
        Implements a single generation.
        """
        self.time += 1
        replacements = []
        for j in range(self.population_size):
            if self.rng.random() < self.death_proba:
                left_parent = self.rng.choice(self.population)
                right_parent = self.rng.choice(self.population)
                # left_parent_index = -1
                # for k in range(self.population_size):
                #     if left_parent is self.population[k]:
                #         left_parent_index = k
                #         break
                # right_parent_index = -1
                # for k in range(self.population_size):
                #     if right_parent is self.population[k]:
                #         right_parent_index = k
                #         break
                # Using integers here just to make debugging easier
                x = self.rng.randint(1, self.sequence_length - 1)
                assert 0 < x < self.sequence_length
                # print(
                #     "BIRTH:",
                #     left_parent,
                #     left_parent_index,
                #     right_parent,
                #     right_parent_index,
                #     j,
                #     x,
                # )
                child = Individual(self.time, self.next_individual_index)
                self.next_individual_index += 1
                child.ancestry = [Segment(0, self.sequence_length, child)]
                replacements.append((j, child))
                record_inheritance(0, x, left_parent, child)
                record_inheritance(x, self.sequence_length, right_parent, child)
                assert len(child.parents) <= 2, child.parents
                self.transmissions.append(
                    TransmissionInfo(
                        left_parent,
                        child,
                        0,
                        x,
                    )
                )
                self.transmissions.append(
                    TransmissionInfo(
                        right_parent,
                        child,
                        x,
                        self.sequence_length,
                    )
                )

        # First propagate the loss of the ancestral material from the newly dead
        # print("pdead")
        for j, ind in replacements:
            dead = self.population[j]
            dead.is_alive = False
            dead.remove_sample_mapping(sequence_length=self.sequence_length)
            if verbose is True:
                print(f"propagating death {dead}")
            propagate_upwards(dead, verbose)
            self.population[j] = ind
        for _, ind in replacements:
            if verbose is True:
                print(f"propagating birth {ind}")
            propagate_upwards(ind, verbose)
        self.check_state()

    def check_state(self):
        reachable = self.all_reachable()
        for ind in reachable:
            if ind.is_alive:
                assert len(ind.ancestry) == 1
                x = ind.ancestry[0]
                assert x.left == 0
                assert x.right == self.sequence_length
                assert x.child == ind
            else:
                assert_non_overlapping(ind.ancestry)
                for a in ind.ancestry:
                    if a.child is not ind and a.child not in reachable:
                        # NOTE: these unary nodes are necessary for mutation simplificaton,
                        # "tskit-style". But they do not contribute to the ancestry of
                        # the alive nodes, so we skip printing a warning for them.
                        # Based on running with -v, these arise when an alive node is removed
                        # from a parent's children but is retained in the ancestry as a unary edge.
                        # This updating removes the ancestor from the alive node's parents set.
                        # Things get "cleaned up" later, the next time simplification works
                        # its way up to the parent node.
                        # This can also happen when the descendant node is alive and present
                        # only in the input ancestry of a parent as a unary edge.  The current
                        # approach maintains this unary status, and it is also eventually cleaned up
                        # by the filter call in the simplification loop.
                        # NOTE: attempts to do things like remove unary edges at the end of simplification leads to
                        # incorrect topologies at the end...
                        # TODO: is there a better way?
                        if a.child.is_alive or len(a.child.children) > 0:
                            print(
                                "UNREACHABLE UNARY", ind, "->", a.child, a.child.parents
                            )
                        # NOTE: I'm keeping this print statement here b/c the comment
                        # block above annoys me -- there must be a better way.
                        print("UNREACHABLE UNARY", ind, "->", a.child, a.child.parents)
            for child, segments in ind.children.items():
                if child is not ind:
                    assert child in reachable, f"{child} {ind} {ind.children}"
                assert_non_overlapping(segments)
                # NOTE: this happens b/c as a side-effect of defaultdict
                if child is not ind:
                    assert ind in child.parents, f"{ind} {child}"
            for parent in ind.parents:
                assert parent in reachable
                if ind not in parent.children:
                    print("the failing parent is")
                    parent.print_state()
                    print("done w/failing parent")
                assert ind in parent.children, f"{ind} {parent}"

    def run(self, num_generations, verbose, simplify_interval=1):
        for _ in range(num_generations):
            self.run_generation(verbose)

        # for i in self.population:
        #     if i.is_alive:
        #         a = i in [t.parent for t in self.transmissions]
        #         b = i in [t.child for t in self.transmissions]
        #         if not a and not b:
        #             self.leftover_alive.append(i)

    def make_samples_list_for_tskit(self, node_map) -> List[int]:
        rv = []
        for i in self.population:
            if i.is_alive and node_map[i.index] not in rv:
                rv.append(node_map[i.index])
        return sorted(rv)
        # for i in self.transmissions:
        #     for j in [i.parent, i.child]:
        #         if j.is_alive and node_map[j.index] not in rv:
        #             rv.append(node_map[j.index])
        # for i in self.leftover_alive:
        #     assert i.is_alive
        #     assert node_map[i.index] not in rv
        #     rv.append(node_map[i.index])
        return sorted(rv)

    def get_alive_node_indexes_and_times(self) -> List[Tuple[int, int]]:
        indexes = set()
        rv = []
        for i in self.population:
            if i.is_alive and i.index not in indexes:
                rv.append((i.index, i.time))
                indexes.add(i.index)
        # for i in self.transmissions:
        #     for j in [i.parent, i.child]:
        #         if j.is_alive and j.index not in indexes:
        #             rv.append((j.index, j.time))
        #             indexes.add(j.index)

        # for i in self.leftover_alive:
        #     assert i.index not in indexes
        #     rv.append((i.index, i.time))
        #     indexes.add(i.index)

        return rv

    def convert_transmissions_to_tables(
        self,
    ) -> Tuple[tskit.TableCollection, List[int]]:
        """
        Take the raw transmission info and make it into
        an unsimplified TableCollection for an independent
        look at the simulated topologies.
        """
        tables = tskit.TableCollection(self.sequence_length)

        # There's probably a faster way to do all this...
        node_data = {}
        for i in self.transmissions:
            for n in [i.parent, i.child]:
                if n.index not in node_data:
                    node_data[n.index] = n.time

        for i in self.population:
            if i.is_alive:
                node_data[i.index] = i.time

        alive_nodes = self.get_alive_node_indexes_and_times()

        assert all([a[0] in node_data for a in alive_nodes])

        node_map = {}
        for i, t in node_data.items():
            x = tables.nodes.add_row(0, -(t - self.time))
            node_map[i] = x

        samples = self.make_samples_list_for_tskit(node_map)

        double_check = sorted([node_map[i[0]] for i in alive_nodes])
        assert samples == double_check, f"{samples} {double_check}"

        for t in self.transmissions:
            tables.edges.add_row(
                t.left, t.right, node_map[t.parent.index], node_map[t.child.index]
            )

        tables.sort()

        return tables, samples, node_map

    def all_reachable(self):
        """
        Returns the set of all individuals reachable from the current populations.
        """
        individuals = set()
        for ind in self.population:
            stack = [ind]
            while len(stack) > 0:
                ind = stack.pop()
                individuals.add(ind)
                for parent in ind.parents:
                    if parent not in individuals:
                        stack.append(parent)
        return individuals

    def export(self):
        """
        Exports the edges to a tskit tree sequence.
        """
        tables = tskit.TableCollection(self.sequence_length)
        # Map the individuals to their indexes to make debug easier.
        # THIS IS A TERRIBLE IDEA!!!
        sorted_individuals = sorted(self.all_reachable(), key=lambda x: x.index)
        # sorted_individuals = [
        #     i
        #     for i in filter(
        #         lambda x: x.is_alive is False
        #         or (len(x.parents) > 0 or len(x.children) > 0),
        #         sorted_individuals,
        #     )
        # ]
        next_ind = 0
        for ind in sorted_individuals:
            while ind.index != next_ind:
                # Add in a padding node.
                tables.nodes.add_row(flags=0, time=0)
                next_ind += 1
            ret = tables.nodes.add_row(
                flags=tskit.NODE_IS_SAMPLE if ind.is_alive is True else 0,
                time=self.time - ind.time,
            )
            assert ret == ind.index
            next_ind += 1

        for ind in sorted_individuals:
            for child, segments in ind.children.items():
                for seg in segments:
                    tables.edges.add_row(
                        left=seg.left,
                        right=seg.right,
                        parent=ind.index,
                        child=child.index,
                    )
        # Can't be bothered doing the sorting above to get rid of this,
        # but it's trivial.
        tables.sort()
        return tables.tree_sequence()


def main():
    parser = make_parser()
    args = parser.parse_args(sys.argv[1:])
    validate_args(args)
    # sim = Simulator(100, 5, death_proba=1.0, seed=seed)
    # sim = Simulator(6, 5, death_proba=1.0, seed=seed)
    sim = Simulator(
        args.N, args.genome_length, death_proba=args.death_probability, seed=args.seed
    )
    # works for 1 generation...
    # sim.run(1)
    sim.run(args.simlen, args.verbose)
    ts = sim.export()
    # print(ts.draw_text())

    topologies = make_topologies(ts)

    tables, samples, node_map = sim.convert_transmissions_to_tables()
    # print(samples)
    idmap = tables.simplify(samples)
    ts_tsk = tables.tree_sequence()

    node_map = {v: k for k, v in node_map.items()}

    # Make node labels which should match what we
    # draw from the new algo.
    node_labels = {}
    for i, j in enumerate(idmap):
        if j != tskit.NULL:
            node_labels[j] = node_map[i]
    # print(ts_tsk.draw_text(node_labels=node_labels))

    tsk_topologies = make_topologies(ts_tsk, node_labels)

    for i, j in zip(topologies, tsk_topologies):
        if i != j:
            print(i)
            print(j)
            assert False

    assert topologies == tsk_topologies


if __name__ == "__main__":
    main()


def test_remove_self_mapping():
    L = 5
    child = Individual(0, 0, is_alive=True)
    child.ancestry = [Segment(0, L, child)]
    assert len(child.ancestry) == 1
    child.remove_sample_mapping(L)
    assert len(child.ancestry) == 0


def test_basics():
    """
    Simple case, easy to reason through.
    """
    pop = []
    L = 5
    for i in range(2):
        parent = Individual(0, i, is_alive=False)
        assert parent.index == i
        pop.append(parent)

    # transmission w/coalescence
    c = Individual(1, len(pop), True)
    c.ancestry.append(Segment(0, L, c))
    record_inheritance(0, L // 2, pop[0], c)
    cc = Individual(1, len(pop) + 1, True)
    cc.ancestry.append(Segment(0, L, cc))
    record_inheritance(L // 3, 3 * L // 4, pop[0], cc)

    # 2x unary edges
    record_inheritance(0, L // 3, pop[1], cc)
    record_inheritance(3 * L // 4, L, pop[1], cc)

    assert pop[0] in c.parents
    assert pop[1] not in c.parents
    assert pop[0] in cc.parents
    assert pop[1] in cc.parents

    e = pop[0].intersecting_ancestry()
    assert len(e) == 2

    propagate_upwards(pop[0], False)

    assert len(pop[0].ancestry) == 3
    assert Segment(0, 1, c) in pop[0].ancestry
    assert Segment(1, 2, pop[0]) in pop[0].ancestry
    assert Segment(2, 3, cc) in pop[0].ancestry

    propagate_upwards(pop[1], False)
    assert len(pop[1].ancestry) == 2
    assert Segment(0, L // 3, cc) in pop[1].ancestry
    assert Segment(3 * L // 4, L, cc) in pop[1].ancestry

    propagate_upwards(c, False)
    propagate_upwards(cc, False)

    assert len(pop[0].ancestry) == 3
    assert Segment(0, 1, c) in pop[0].ancestry
    assert Segment(1, 2, pop[0]) in pop[0].ancestry
    assert Segment(2, 3, cc) in pop[0].ancestry

    assert len(pop[1].ancestry) == 2
    assert Segment(0, L // 3, cc) in pop[1].ancestry
    assert Segment(3 * L // 4, L, cc) in pop[1].ancestry


def failing_case_1():
    # seed = 1, N=4, L=5, using basically Jerome's prototype
    pop = []
    L = 5
    for i in range(4):
        parent = Individual(0, i, is_alive=False)
        assert parent.index == i
        pop.append(parent)

    next_index = len(pop)

    replacements = []
    x = [1, 4, 1, 1]  # xover positions
    parents = [(0, 2), (3, 3), (0, 3), (3, 3)]
    for i in range(4):
        child = Individual(1, next_index)
        next_index += 1
        child.ancestry.append(Segment(0, L, child))
        record_inheritance(0, x[i], pop[parents[i][0]], child)
        record_inheritance(x[i], L, pop[parents[i][1]], child)
        replacements.append((i, child))
    for j, ind in replacements:
        dead = pop[j]
        dead.is_alive = False
        # NOTE: EXPERIMENTAL
        dead.remove_sample_mapping(sequence_length=L)
        propagate_upwards(dead, False)
        pop[j] = ind

    for _, ind in replacements:
        # print("replacement")
        # ind.print_state()
        propagate_upwards(ind, False)

    return pop


def failing_case_2():
    # seed = 2, N=4, L=5, using basically Jerome's prototype
    pop = []
    L = 5
    for i in range(4):
        parent = Individual(0, i, is_alive=False)
        assert parent.index == i
        pop.append(parent)
    next_index = len(pop)

    replacements = []
    x = [1, 3, 4, 3]  # xover positions
    parents = [(0, 0), (1, 2), (0, 1), (2, 3)]
    for i in range(4):
        child = Individual(1, next_index)
        next_index += 1
        child.ancestry.append(Segment(0, L, child))
        record_inheritance(0, x[i], pop[parents[i][0]], child)
        record_inheritance(x[i], L, pop[parents[i][1]], child)
        replacements.append((i, child))
    for j, ind in replacements:
        dead = pop[j]
        dead.is_alive = False
        # NOTE: EXPERIMENTAL
        dead.remove_sample_mapping(sequence_length=L)
        propagate_upwards(dead, False)
        pop[j] = ind

    for _, ind in replacements:
        # print("replacement")
        # ind.print_state()
        propagate_upwards(ind, False)

    return pop


def collect_unique_individuals(pop):
    individuals = set()
    for i in pop:
        stack = [i]
        while len(stack) > 0:
            j = stack.pop()
            individuals.add(j)
            for p in j.parents:
                stack.append(p)
    return individuals


def test_failing_case_1():
    pop = failing_case_1()
    individuals = collect_unique_individuals(pop)

    parent_indexes = [(), (), (), (), (0,), (3,), (3, 0), (3,)]

    for i in individuals:
        assert len(i.parents) == len(parent_indexes[i.index]), f"{i} -> {i.parents}"
        for p in i.parents:
            assert p.index in parent_indexes[i.index]

    for i in individuals:
        if i.index > 3:
            assert i.is_alive
            assert len(i.ancestry) == 1
        else:
            assert not i.is_alive

            assert i.index != 1
            assert i.index != 2

            if i.index == 0:
                assert len(i.ancestry) == 1
                assert i.ancestry[0].child is i
                assert i.ancestry[0].left == 0
                assert i.ancestry[0].right == 1

                found4 = False
                found6 = False
                for child, segments in i.children.items():
                    if child.index == 4:
                        assert Segment(0, 1, None) in segments
                        found4 = True
                    elif child.index == 6:
                        assert Segment(0, 1, None) in segments
                        found6 = True
                    elif child.index == i.index:
                        pass
                    else:
                        assert False, f"{child}"

                assert found4 is True
                assert found6 is True

            if i.index == 3:
                i.print_state()
                assert len(i.ancestry) == 3
                segs = [(0, 1), (1, 4), (4, 5)]
                for a, s in zip(i.ancestry, segs):
                    assert a.left == s[0]
                    assert a.right == s[1]
                    assert a.child is i

                found5 = False
                found6 = False
                found7 = False
                for child, segments in i.children.items():
                    if child.index == 5:
                        assert Segment(0, 1, None) in segments
                        assert Segment(1, 4, None) in segments
                        assert Segment(4, 5, None) in segments
                        found5 = True
                    elif child.index == 7:
                        assert Segment(0, 1, None) in segments
                        assert Segment(1, 4, None) in segments
                        assert Segment(4, 5, None) in segments
                        found7 = True
                    elif child.index == 6:
                        assert Segment(1, 4, None) in segments
                        assert Segment(4, 5, None) in segments
                        found6 = True
                    elif child.index == i.index:
                        pass
                    else:
                        pass
                        assert False, f"{child}"

                assert found5 is True, "A"
                assert found6 is True, "B"
                assert found7 is True, "C"


def test_failing_case_2():
    pop = failing_case_2()
    individuals = collect_unique_individuals(pop)

    parent_indexes = [(), (), (), (), (0,), (), (0,), ()]

    # for i in individuals:
    #     if i.index == 0:
    #         i.print_state()
    #     print("CHILDREN")
    #     for j in i.children:
    #         j.print_state()

    for i in individuals:
        assert len(i.parents) == len(parent_indexes[i.index]), f"{i} -> {i.parents}"
        for p in i.parents:
            assert p.index in parent_indexes[i.index]


def test_failing_case_2_subtree():
    """
    The "problem" involves node 0 and its 2 offspring.

    This test only records inheritances from 0 to the 2 offspring.
    """
    pop = []
    L = 5
    for i in range(4):
        parent = Individual(0, i, is_alive=False)
        assert parent.index == i
        pop.append(parent)

    next_index = len(pop)

    replacements = []
    x = [1, 3, 4, 3]  # xover positions
    parents = [(0, 0), (1, 2), (0, 1), (2, 3)]
    for i in range(4):
        child = Individual(1, next_index)
        next_index += 1
        child.ancestry.append(Segment(0, L, child))
        p1 = False
        p2 = False
        if parents[i][0] == 0 or parents[i][0] == 1:
            p1 = True
        if parents[i][1] == 0 or parents[i][1] == 1:
            p2 = True
        if parents[i][0] == 0 or parents[i][1] == 0:
            record_inheritance(0, x[i], pop[parents[i][0]], child)
            record_inheritance(x[i], L, pop[parents[i][1]], child)
        replacements.append((i, child))

    print("propagate deaths")
    for j, ind in replacements:
        dead = pop[j]
        dead.is_alive = False
        # NOTE: EXPERIMENTAL
        dead.remove_sample_mapping(sequence_length=L)
        propagate_upwards(dead, False)
        pop[j] = ind

    print("propagate replacements")
    for _, ind in replacements:
        # print("replacement")
        # ind.print_state()
        propagate_upwards(ind, False)

    individuals = collect_unique_individuals(pop)

    parent_indexes = [(), (), (), (), (0,), (), (0,), ()]

    for i in individuals:
        assert len(i.parents) == len(parent_indexes[i.index]), f"{i} -> {i.parents}"
        for p in i.parents:
            assert p.index in parent_indexes[i.index]


def test_failing_case_1_next_generation():
    L = 5
    pop = failing_case_1()

    next_index = max([i.index for i in collect_unique_individuals(pop)]) + 1

    replacements = []
    x = [1, 1, 2, 4]  # xover positions
    parents = [(2, 1), (0, 0), (0, 3), (0, 1)]
    for i in range(4):
        child = Individual(2, next_index)
        next_index += 1
        child.ancestry.append(Segment(0, L, child))
        record_inheritance(0, x[i], pop[parents[i][0]], child)
        record_inheritance(x[i], L, pop[parents[i][1]], child)
        replacements.append((i, child))
    for j, ind in replacements:
        dead = pop[j]
        dead.is_alive = False
        # NOTE: EXPERIMENTAL
        dead.remove_sample_mapping(sequence_length=L)
        propagate_upwards(dead, False)
        pop[j] = ind

    for _, ind in replacements:
        # print("replacement")
        # ind.print_state()
        propagate_upwards(ind, False)
