"""S1: the synthetic ACL corpus for the filtered-vector-search spike (design §13).

``generate`` streams a deterministic dataset to Parquet; ``load`` copies it into Postgres.
Every row is a pure function of (seed, row index), so ``batch_rows`` never changes the
data or ``content_sha256``. Synthetic data only (D20).
"""

from cairn_eval.spikes.s1_acl_corpus.config import S1Config, principal_names
from cairn_eval.spikes.s1_acl_corpus.generate import (
    DatasetError,
    Manifest,
    OutputDirectoryNotEmpty,
    generate,
    read_manifest,
)
from cairn_eval.spikes.s1_acl_corpus.load import load

__all__ = [
    "DatasetError",
    "Manifest",
    "OutputDirectoryNotEmpty",
    "S1Config",
    "generate",
    "load",
    "principal_names",
    "read_manifest",
]
