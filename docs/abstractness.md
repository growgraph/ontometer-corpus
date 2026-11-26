# Abstractness

This page defines how the corpus measures the abstractness of a term and of an ontology,
from how the corpus uses it, and how the network of ontologies is replayed over time.
After reading it you can read every column of the abstractness tables back to its
definition and know which ontologies are measured and why.

Semantic-web practice draws a binary line: an *ontology* holds abstract classes, *facts* hold
concrete instances. Across a corpus that line is not where anyone drew it. The same IRI is a
class in one file and an individual in another; a species is a leaf in one hierarchy and the
root of thousands of strains in another; a class declared by one ontology becomes the
superclass of hundreds of others. The corpus measures abstractness as a **continuous** quantity,
from how the corpus actually uses each term.

This page describes the definitions and the mechanism. The commands are in
[Rebuilding](rebuild.md#stage-4-abstractness-and-growth).

## Two senses, never blended

| Sense | Question | Opposite |
|---|---|---|
| **Generality** | How much of the world does the term subsume? | a specific leaf class |
| **Class-likeness** | Is the term used as a class or as an instance? | a concrete individual |

A leaf class is maximally specific and wholly class-like. A unit such as *metre* is a leaf
modelled as a class in some ontologies and as an individual in others. The two senses are
reported side by side; a composite would need evidence that they measure one thing.

### Generality

Computed on the corpus-wide subsumption DAG of terms, and on each ontology's own hierarchy
([`ontometer_corpus.metrics.abstractness`](reference/metrics/abstractness.md)):

| Signal | Definition |
|---|---|
| `generality` | `1 − IC`, with intrinsic information content after Seco et al.: `IC = 1 − log(descendants + 1) / log N` |
| `ic_sanchez` | Sánchez et al.: leaves below over subsumers above, normalised |
| `depth_global_long`, `depth_global_short` | longest and shortest path from a root |
| `df_spec` | number of *foreign* ontologies that specialise the term |
| `div_comm` | effective number (`exp` of the entropy) of reuse communities among those specialisers |

Descendant counts and depth are different quantities on a DAG with many roots: depth counts the
layers *above* a term (how anchored it is), descendants the breadth *below* it. Descendant
counts are exact. Cycles are condensed first, and ancestor sets are propagated rather than
summed over children, because a sum over children counts paths and over-counts at every
diamond.

### Class-likeness

A role pass reads every file and records the roles each term plays
([`ontometer_corpus.roles`](reference/roles.md)):

- **class roles**: declared `owl:Class` or `rdfs:Class`; either end of a named
  `rdfs:subClassOf`; object of `rdf:type` (it has instances); domain, range, restriction filler,
  equivalence, disjointness or complement operand;
- **instance roles**: declared `owl:NamedIndividual`; subject of `rdf:type` with a non-meta
  type or `owl:Thing`; subject or object of a *fact*.

A fact is a triple between two IRIs whose predicate is neither language vocabulary nor
annotation. Annotation is recognised by namespace, by `owl:AnnotationProperty` declaration and
by subject (the ontology header); without that, metadata would make ordinary classes look like
instances.

`role_share = df_class / (df_class + df_inst)` over the documents that use the term. A term is
*punned* when one file uses it in both roles. Counting documents rather than triples keeps one
large knowledge base from outvoting the rest of the corpus.

### Popularity is kept apart

`df_decl_foreign` (foreign documents declaring the term) and `df_ref_foreign` (foreign
documents mentioning it) are computed and reported separately. Being *specialised* by other
ontologies is evidence of generality; being *copied* by them is not.

## Ownership and edge authority

Reuse counts are only meaningful once it is settled who owns a term and who asserted an edge
([`ontometer_corpus.network.entities`](reference/network/entities.md)).

- **Home.** Every ontology declaring terms in a namespace claims it. Claims are settled by an
  ontology IRI that names the namespace (for OBO, also the namespaces under its PURL folder),
  else by a claimant declaring at least ten times as many terms there as any other; otherwise
  the namespace has no home in the corpus.
- **Edge authority.** An asserted `subClassOf (c, p)` in ontology O is:
    - **own-internal** when O is the home of both;
    - **own-foreign** when O is c's home and p is homed elsewhere, which makes it a
      *specialisation* of p's home;
    - **bridge** when p is homed in O but c is not;
    - **copy** otherwise (an import closure or copied axiom), which is never counted as reuse.
- The **term DAG** is the union of own and bridge edges, plus copies about terms no corpus
  ontology owns: for those, the copies are the only evidence of their hierarchy.
- Language and tooling vocabulary (RDF, RDFS, OWL, XSD, `oboInOwl`, Protégé, SWRL) is never a
  term.

## Ontology level

- **From its classes**: `A_local`, the mean generality of an ontology's own classes inside its
  own hierarchy; `A_global`, the same in the corpus DAG; `A_class`, the mean class-likeness of its
  own terms; `upper_share`, the share of its classes that some foreign ontology specialises.
- **From its dependencies**, with Martin's package metrics transposed:
    - `Ca`: the ontologies that specialise at least one of its classes;
    - `Ce`: the ontologies whose classes it specialises;
    - `I = Ce / (Ca + Ce)`;
    - `D = |A + I − 1|`, the distance from the main sequence;
    - `layer`: the longest path from an ontology that specialises nobody.

`A_global` counts foreign descendants, which is also what `Ca` counts, so its agreement with `I`
is partly built in; `A_local` is the independent estimate.

**Population.** The unit is one ontology per *release family*
([`ontometer_corpus.versions`](reference/versions.md)): registries that number their
submissions put the family before the last `::` of the fetch identifier (`bp:PTO::355` →
`bp:PTO`), and only the latest release of each family is measured; copies of one ontology
from several registries count once, at their newest release (dedup Pass D). SKOS-dominated
sources (thesauri, and hybrids with at least as many SKOS concepts as classes) are not in the
term network at all: they have no OWL class hierarchy to measure, and stay in the dependency
network instead. Ontologies with a class hierarchy
and no more individuals than classes are the **measured ontologies** (the `subject` column of
the ontology table). Knowledge-base-like and flat sources are not measured but still contribute
evidence, since they are where instance roles appear. Individuals here are declared and typed
ones alike, but not SKOS concepts: a concept scheme's concepts are its vocabulary, so for this
split they count as terms (`ontometer_corpus.roles.kb_individuals`).

## Over time: the ontology DAG G(t)

The specialisation network of ontologies is a function of time. As new ontologies arrive,
some older ones that nothing specialised (terminal ontologies, leaves of the network) get
specialised and become more abstract in the network sense. Specialiser counts can only grow, so
that is true of each ontology by construction. The questions with content are how fast leaves
turn into branches, whether new specialisations spread out or concentrate, what predicts
adoption, and whether the corpus as a whole becomes more abstract.

### Placing a snapshot in time

A corpus is one capture, so G(t) is rebuilt by dating
([`ontometer_corpus.dating`](reference/dating.md)). [Dates](dates.md) describes the
sources, the rules and the tables that store them.

- **First evidence.** No source dates an ontology's birth. Registry releases, the web
  archive's first capture of the IRI and a Wikidata inception date are each an upper bound on
  it, and the earliest wins.
- **Link arrival.** A specialisation pair (child ontology, parent ontology) arrives at the first
  release of the child that asserts it when the child's release history is in the release panel,
  and otherwise at the child's first evidence. It never arrives before its parent.
- **Bounded birth.** A class exists before anything specialises it, so an ontology is born no
  later than the first *observed* specialisation of one of its classes (`bound_births`). The
  bound uses observations only and is not chained, so an assumed link date never moves a
  parent's birth.

### What is measured

`ontometer-corpus growth`, over [`ontometer_corpus.metrics.growth`](reference/metrics/growth.md):

1. **Leaf survival**: time from birth to the first specialiser, Kaplan–Meier, censored at the
   capture. Ontologies already specialised when first attested are counted apart, because their
   leaf phase is unobserved.
2. **Cohorts against the corpus**: per year, the share of ontologies that are branches, the
   network generality `log(1 + transitive specialisers) / log N(t)` and the height (the longest
   chain of specialisers beneath), for the whole G(t) and for fixed birth cohorts. A shift-share
   decomposition separates change within the ontologies already present from the arrival of new
   ones.
3. **Attachment nulls**: the dated arrivals replayed with random targets among the ontologies
   existing at each arrival, either uniformly or in proportion to one plus the specialisations
   received so far. Fewer observed conversions than under preferential attachment means adoption
   selects on something beyond accumulated popularity.
4. **Predictors of adoption**: content abstractness (`A_local`), size and registry of the
   ontologies specialised within five years of birth, against those that were not.
5. **Class-likeness over time**: for terms used by other ontologies, ordered by the users'
   first evidence, promotions (the earliest users use a term as an instance, a later one as a
   class) against demotions, tested against a permutation of arrival order within each term.
   Terms of one owner share their users, so a sign test over owning ontologies is the one to
   read.

The release panel (`ontometer-corpus panel`) answers the separate question of whether one
ontology's own content drifts across its releases. It compares each release with a growth-only
null model that attaches the same number of classes as random leaves.

### Drawing G(t)

The [abstractness race](growth.md), the G(t) explorer, replays the dated network in the Poincaré disk
([`ontometer_corpus.growth_network`](reference/growth_network.md),
[`ontometer_corpus.network.hyperbolic`](reference/network/hyperbolic.md)). It is
rebuilt from three small tracked tables, so it needs no object store. The layout is
constructed, not learned:

- **Angle**: fixed from a radial tree over each ontology's *primary parent* (the ontology it
  specialises most, with cycles cut), roots ordered by reuse community. An ontology never moves
  sideways.
- **Radius**: a hyperbolic distance that shrinks with the number of ontologies specialising it
  by time t, `r = r_max · (1 − log(1 + s) / log n)`, mapped to the disk as `tanh(r / 2)`. `n` is a
  fixed normaliser, so an ontology moves inward exactly when it gains a specialiser.

## Gaps

`ontometer-corpus validation` produces four ranked candidate lists of gaps:

1. **Missing shared abstraction**: a label declared as a class in several homes, where no
   single ancestor in the term DAG covers more than a small share of the declarations (low
   *cover*).
2. **Missing middle**: foreign specialisations whose generality jump exceeds the 99th
   percentile of the jumps inside ontologies, i.e. domain classes hung directly under an upper
   ontology's top.
3. **Unanchored ontologies**: measured ontologies that specialise nothing foreign, and are
   fully isolated when nothing specialises them either.
4. **Instances without a class, and role conflicts**: terms used only as instances, and terms
   used as a class in some documents and an instance in others.

## References

- Seco, Veale & Hayes (2004), *An intrinsic information content metric for semantic similarity
  in WordNet*.
- Sánchez, Batet & Isern (2011), *Ontology-based information content computation*.
- Martin (1994), *OO design quality metrics: an analysis of dependencies*.
- Zhang, Kamdar, Tudorache & Musen (2017), term reuse and overlap across BioPortal.
- Hoffman et al. (2013), *Semantic diversity*.
- Asprino, Basile, Ciancarini & Presutti (2018), class and instance identification in Linked
  Open Data.
- Nickel & Kiela (2017), *Poincaré embeddings for learning hierarchical representations*.

## What to read next

- [Dates](dates.md): where the birth and arrival dates behind G(t) come from.
- [Abstractness race](growth.md): the dated network, replayed in the Poincaré
  disk.
- [Rebuilding](rebuild.md#stage-4-abstractness-and-growth): the commands
  that compute all of this.
