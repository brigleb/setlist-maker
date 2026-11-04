# GPL-3, because the fingerprinter is SongRec's

Hindsight is licensed GPL-3, not MIT like the Python version. Shazam fingerprinting is ported
from SongRec's `SignatureGenerator` (GPL-3), credited in `LICENSE` and the module docs.
shazamio-core ships the same generator (its field names and Hann table) under an MIT label, so
porting that crate instead would only inherit the muddle. SongRec is also the project to watch
if Shazam changes its endpoint. Only the centred 10s excerpt and the 16 kHz mono resampling
come from shazamio-core, reimplemented to match it, because every offset rule in the engine
depends on them. Ray is the sole author, so relicensing needed no one else's consent; once
others contribute under GPL-3, going back to MIT would.
