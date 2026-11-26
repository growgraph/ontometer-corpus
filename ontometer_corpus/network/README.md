# Dependency network

`ontometer_corpus/network/construction.py` builds the ontology dependency network from
`corpus.db` and the structural sidecars. This page defines the graph, where each part comes
from, and how import and usage targets are matched to corpus ontologies.

<!-- --8<-- [start:body] -->
```bash
ontometer-corpus network --corpus /path/to/data --store /path/to/store
```

```python
from ontometer_corpus.network.construction import load_network

G = load_network("data")  # networkx.MultiDiGraph
```

## 1. What is built

A directed multigraph `G = (V, E)`:

- **V**: one node per ontology in the corpus, plus one per *external* target that does not
  resolve to a corpus ontology (§3).
- **E**: directed edges `source → target`, each with `edge_type ∈ {import, usage}` (§2). A
  pair can be linked by an import edge, a usage edge, or both, so `G` is a
  `networkx.MultiDiGraph`. Which of the two exist for a pair is itself a signal (§5).

Construction computes no metrics. Every measure is a function run over `G` after loading it.

## 2. Nodes and edges

### Nodes

`V = V_corpus ∪ V_external`

- **`V_corpus`**: one node per row of `corpus_registry` with `is_latest_version = 1` and
  `parse_status = 'ok'` (which implies `is_duplicate = 0`; see the dedup phase in
  `ontometer_corpus/pipeline/README.md`). The node id is the row's `ontology_iri`, or `row:<id>`
  for a row without one or whose IRI is shared by distinct ontologies (`shared_iri`;
  `construction.node_id_of`). Attributes: `source_registry` (where the kept copy was fetched),
  `home_registry` (the registry the ontology belongs to; `ontometer_corpus.registries`), `file_hash`, `content_hash`, `triple_count`,
  `class_count`, `property_count`, `individual_count`, `own_namespaces`,
  `referenced_namespaces`, `imports`, and `is_external = False`. `individual_count` counts
  typed individuals and SKOS concepts as well as declared ones (see "What parsing writes" in
  `ontometer_corpus/pipeline/README.md`).
- **`V_external`**: one node per import or usage target that resolves to no corpus node: an
  ontology the corpus references but does not contain. The node id is `external:<target>`;
  attributes are `is_external = True` and `external_iri`, the unresolved target.

### Edges

`E = E_import ∪ E_usage`, both directed `ontology → target`:

- **`E_import`**: one edge per source-target relation represented in the row's `imports` column
  (its `owl:imports` targets), resolved only when the normalized IRI identifies one corpus node
  (§4). Its `resolution` attribute records how.
- **`E_usage`**: one edge per source-target relation represented by namespaces in the row's
  `referenced_namespaces` that resolves to
  another ontology, i.e. a namespace whose terms it uses but does not own. Ownership is settled
  across all claimants (see "Owners of contested namespaces"), so a namespace the row only
  claims because it declares copied terms (MIREOT, baked imports) still yields an edge to its
  owner.

Both edge sets come from columns the parse phase already fills. No parsing happens here.

## 3. External nodes

Many import and usage targets point outside the corpus: vocabularies no registry holds, or
ontologies that were never fetched. Such an edge is kept, and its target becomes an external
node. Which outside dependencies the corpus leans on, and which ontologies reference things
nobody retrieved, is itself worth measuring.

An external node is created only when no trustworthy corpus row owns the target (§4).

External ids carry the `external:` prefix so they can never collide with a real
`ontology_iri`. A corpus row can carry a degenerate IRI, such as the bare OBO root
`http://purl.obolibrary.org/obo/`. Without the prefix, every unresolved reference to that same
string would land on that row's node and inflate its in-degree.

## 4. Matching targets to corpus ontologies

### `normalize_iri`: import targets

Import targets often name a versioned or module-specific URL rather than the ontology IRI.
`normalize_iri(iri)`:

1. expands unexpanded XML entities (`&obo;` → the OBO Foundry base IRI), which appear when a
   source file never resolved its own `<!ENTITY>` declaration;
2. strips import-module suffixes: `.../imports/chebi_import.owl` → `.../`;
3. collapses version path segments: `.../releases/<ver>/foo.owl` and `.../<DATE>/foo.owl` →
   `.../foo.owl`.

Only a unique normalised IRI match resolves an import to a corpus ontology. A final filename
such as `core.owl` is not identity evidence, even when it occurs only once in this corpus:
unmatched imports stay external rather than being guessed onto a corpus node. Import edges
record `resolution` as `exact_iri`, `ambiguous_iri`, or `external`. A match that would point
back to its source is an internal module import and is omitted from the inter-ontology graph.

### `_ns_key`: usage targets

A referenced namespace usually ends in `#` or `/`, and the owning ontology's IRI usually does
not. `_ns_key(iri) = normalize_iri(iri).rstrip("/#")`. A usage lookup tries the raw namespace
first, then its `_ns_key`.

### Owners of contested namespaces

Every row claims the namespaces in its `own_namespaces`, and its own IRI (as an `_ns_key`).
Many namespaces have several claimants, because a file that copies another ontology's terms
declares them just as the original does. Claims are settled by the rule the term network uses,
`ontometer_corpus.network.entities.pick_home`, so both networks agree on who owns a namespace:

1. the claimant whose ontology IRI names the namespace: `.../obo/bfo.owl` owns `.../obo/BFO_`,
   and an OBO ontology also owns the namespaces under its PURL folder (`.../obo/ro.owl` owns
   `.../obo/ro/subsets#`). If several do, the one declaring most terms in it. A SKOS scheme may
   override this only when the named claimant declares no terms in the namespace and the scheme
   has at least ten directly typed concepts there, ten times the named claimant's count;
2. otherwise a *dominant* declarer, one declaring at least ten times as many entities in the
   namespace as any other (`DOMINANCE`), counted from the structural sidecars. GAZ owns
   `.../obo/GAZ_` this way, although its file declares no ontology IRI;
3. otherwise nobody: the namespace stays unresolved, and its usages become external nodes.

Rule 3 is what namespaces that are no ontology's need (ORCID iDs and DOIs that files declare
as individuals, licence URLs) and vocabularies outside the corpus that several files copy from
(`http://dbpedia.org/resource/`). Giving them to the largest copier would credit that file with
every use: before this rule, one file listing its citations as individuals owned 234 DOI and
library namespaces. Registry provenance plays no part, since a copier listed by the "right"
registry would otherwise beat a real owner listed elsewhere.

**SKOS concept schemes.** A namespace claimant carries its count of directly typed
`skos:Concept`s in the namespace being decided, read from its structural sidecar (with the
term-role artifact as a compatibility fallback). Counts from other namespaces in the same file
play no part. The exception applies only when the claimant whose IRI names the namespace declares
no terms there.
This deliberately narrow exception covers schemes whose ontology IRI names a schema rather
than their concept namespace: AGROVOC's concept population correctly beats LANDVOC's empty
declaration claim for `http://aims.fao.org/aos/agrovoc/`. A thesaurus that merely copies RO,
BFO or IAO terms cannot displace those named owners, regardless of how many concepts it holds
elsewhere.

Namespaces classified as metadata or identifiers (licences, ORCID, DOI and Wikimedia resource
URLs) never receive a corpus owner. Their evidence remains visible as an external usage relation.

A row whose IRI dedup found shared by distinct ontologies (`shared_iri = 1`) is identified by
`row:<id>`, and its IRI names no namespace. Modelling-language namespaces
(`ontometer_corpus.roles.is_meta`: SWRL, oboInOwl, …) are never owned.

### Popular vocabularies and the representation languages

There is no list of "generic" vocabularies. Dublin Core, SKOS, FOAF, PROV, schema.org and the
rest are ontologies like any other: the row whose IRI names the namespace owns it, whichever
registry dedup kept it from, and a file that merely declares or copies their terms is a
claimant that loses to that named owner (rule 1). A thesaurus or plugin bundle that misdeclares
a well-known IRI loses to the claimant that declares the vocabulary's terms. If the
vocabulary's own ontology is not in the corpus and no claimant dominates, its namespace stays
external.

Only the representation languages are set apart, by specification rather than popularity:
RDF, RDFS, XSD, the XML namespace, OWL, SWRL, the OBO-format mapping (oboInOwl) and Protégé's
bookkeeping (`LANGUAGE_NAMESPACES` in `ontometer_corpus/namespaces.py`). They are never owned:
using `rdf:type` is writing in a language, not reusing an ontology. The parse phase removes
them, and the bare OBO root, from every row's `own_namespaces` (`UNOWNABLE_NAMESPACES`), and
their usage edges point at external nodes labelled `language`.

Whether an ontology uses a vocabulary for modelling or only for metadata is measured per use,
not decided by namespace (§5).

A usage edge that resolves back to its own source is dropped: an ontology using its own terms
would otherwise loop onto itself.

**OBO term IDs** are handled one level down, in `iri_namespace`
(`ontometer_corpus/namespaces.py`). `GO_0008150`, `CHEBI_36357` and `CL_0000000` share the path
`http://purl.obolibrary.org/obo/`; their identity is the `PREFIX_` in the local name. Well-formed
`PREFIX_localid` names get their own namespace (`.../obo/GO_`, `.../obo/CHEBI_`), and the bare
OBO root is excluded from every ontology's `own_namespaces`, since no single ontology owns it.
A malformed local name falls back to the bare root, which becomes an external node: a coarse
record that something in the shared OBO ID space was referenced.

## 5. Provenance

| Part of the graph | Source columns | Resolution |
|---|---|---|
| Node identity and scalars | `ontology_iri`, `source_registry`, `file_hash`, `content_hash`, `triple_count`, `class_count`, `property_count`, `individual_count` | copied |
| `E_import` | `imports` (JSON array of `owl:imports` IRIs) | unique normalized IRI; an OBO import module or dated release (`…/obo/foodon/imports/chebi_import.owl`, `…/obo/iao/2017-03-24/iao.owl`) resolves to the ontology it extracts (`resolution = "obo_module"`); ambiguous or unmatched targets stay external |
| `E_usage` | `referenced_namespaces` (JSON array), minus namespaces the row owns | namespace, then `_ns_key`, against the owners settled from all rows' `own_namespaces` by `pick_home` |

Two kinds of dependency follow:

- **Declared**: what an ontology says it depends on, through `owl:imports`.
- **Actual**: what it uses. `referenced_namespaces` is collected by walking every triple, in
  every parser path, not by reading prefix declarations, so a namespace counts only when a term
  from it occurs.

The **declared-versus-actual gap** is then a comparison of the two edge types: a usage edge to a
target with no import edge is a dependency used without being declared. With both edge types in
one multigraph, that is a direct query over parallel edges.

Every edge represents one `(source, target, edge_type)` relation. `reference_iris` retains every
distinct raw IRI or namespace that produced it, `evidence_count` gives the number of such values,
and `reference_iri` remains as the deterministic first value for compatibility. This prevents
multiple spellings or namespaces from inflating graph degree while retaining their evidence.
Usage edges are labelled `domain_vocabulary`, `language`, `metadata_or_identifier`, or
`malformed_or_local` by namespace (`reference_kind`). A namespace cannot say how a vocabulary
is used, so usage edges also carry term-level evidence from `namespace_usage.parquet`, written
by `ontometer-corpus term-coupling` from a re-read of each file (`ontometer_corpus.usage`): the distinct
terms the source uses from the target **logically** (`n_logical_terms`: subclass or
subproperty ends, domain and range, restriction properties and fillers, class-expression
members, types of non-header subjects, property assertions), **as annotation**
(`n_annotation_terms`: annotation properties, statements about the ontology header or an axiom
annotation, literal-valued or class-level statements through undeclared predicates), or only
in declarations (`n_declared_terms`). `dcterms:creator` on the header is annotation; between
two individuals it is logical. The columns are null where the file was not re-read (size guard
or timeout). External nodes carry the corresponding `external_kind` where it is clear;
otherwise they remain `unclassified`. External targets that differ only by a trailing `/` or
`#` are one node. The same `reference_kind` classifier defines the domain-only denominator in
`term_coupling.parquet`, and its `n_terms_logical` / `n_ext_logical` use the same per-use
evidence, so the graph and the coupling table agree on what counts as reuse.

## 6. Files

| File | Content |
|---|---|
| `ontometer_corpus/network/construction.py` | `normalize_iri`, `build_network`, `save_network`, `load_network` |
| `corpus/network_nodes.parquet` | one row per node, list attributes as JSON |
| `corpus/network_edges.parquet` | one row per source-target edge type, with `reference_iris`, `reference_kinds`, `resolutions`, `evidence_count`, and compatibility scalar fields |

The graph is stored as two Parquet tables rather than a pickle, because loading a pickle runs
whatever code it contains, and rather than GraphML or GML, which cannot hold list attributes.
`load_network` rebuilds the `MultiDiGraph` exactly.

<!-- --8<-- [end:body] -->
