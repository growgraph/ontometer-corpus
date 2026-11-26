# Fetch, parse, deduplicate

This page describes the first two stages of the corpus pipeline: **fetch**,
which downloads ontology files from the registries, and **process**, which
seeds, parses and deduplicates them in three phases. Everything downstream, the
[dependency network](network.md), the metrics and
[abstractness](abstractness.md), reads what these stages write. After reading
it you know what each phase stores, how a run resumes, and which rules decide
that two files are one ontology. The end-to-end map and the reproduction
commands are in [Overview](overview.md) and [Rebuilding](rebuild.md).

--8<-- "ontometer_corpus/pipeline/README.md:body"
