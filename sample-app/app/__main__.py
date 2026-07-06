"""Sample checkout service entry point.

`python -m app` starts the Flask server. That is the only mode — verification
load is driven externally by the verifier, exactly as production traffic
would be. The application has no knowledge that a verifier exists.
"""

from .server import run

if __name__ == "__main__":
    run()
