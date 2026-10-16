"""Names, sizes and format identifiers shared across GhostVault."""

from . import __version__

APP_NAME = "GhostVault"

CHUNK_SIZE = 512 * 1024           # plaintext bytes per chunk
BASE_FOLDER = "ghostvault"        # folder GhostVault owns inside each repository
SETTINGS_VERSION = 5              # settings.json format (keys added since are optional)
MANIFEST_VERSION = 6              # written for new ghosts (padding, length-bound chunks, previews)
MANIFEST_VERSIONS = (5, 6)        # read: 5 was written by earlier versions; never migrated
MANIFEST_FORMAT = "ghostvault-manifest"
SETTINGS_FORMAT = "ghostvault-settings"
USER_AGENT = f"GhostVault/{__version__}"
GITHUB_API = "https://api.github.com"
COMMIT_MESSAGE = "Update"         # neutral: commit messages say nothing about the files
KEYCHAIN_SERVICE = "GhostVault"
