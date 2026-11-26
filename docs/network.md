# Dependency network

The dependency network is built from `corpus.db` and the structural sidecars
that [parsing](pipeline.md) writes. This page defines the graph, where each
node and edge comes from, and how import and usage targets are matched to
corpus ontologies. After reading it you can read an edge of the network back
to the triples that produced it, and know when a target stays external.

--8<-- "ontometer_corpus/network/README.md:body"
