from typing import Tuple
from .codfreq_types import NAPos, NAChar


PosNA = Tuple[
    NAPos,   # refpos
    int,     # insertion_index
    NAChar,  # na
    int      # qua
]
