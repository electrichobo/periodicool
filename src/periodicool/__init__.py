"""PeriodiCool: self-hosted manager for magazines."""

__version__ = "0.2.1"
# Sent with every request to Prowlarr, SABnzbd and the metadata sources. Some
# sites (Cloudflare in front of indexers) refuse Python's default "Python-urllib".
USER_AGENT = f"PeriodiCool/{__version__} (+https://github.com/electrichobo/periodicool)"
