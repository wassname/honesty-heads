"""Local replication pipeline. PI/OpenAI."""

import os

if os.environ.get("BEARTYPE"):
    from beartype.claw import beartype_this_package

    beartype_this_package()
