# Board cleanup through order publish (#1655 part C)

Jin decided on 2026-11-02 that /fa proposes board cleanup and applies each item only after approval. PM settled on 2026-11-04 that `fa order publish` is the only writer of these closes (one writer, like `replaces`). Contract: plan field `closes` with items `{number, commit}` or `{number, into}`.
