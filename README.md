# local-ai-video-clipper

## YouTube authentication on Streamlit Community Cloud

The app reads downloader configuration from Streamlit Secrets when the app starts. It does not ask visitors to upload cookie files or encode cookies as base64. In the app's Streamlit Cloud settings, open **Settings → Secrets** and add the following TOML, replacing the placeholder with the raw Netscape-format cookie export:

```toml
YTDLP_COOKIES = """# Netscape HTTP Cookie File
# Paste the cookie export lines here; do not commit them to Git.
"""

YTDLP_PLAYER_CLIENTS = "ios,android,web_safari,default,web_embedded,mweb,web,visionos"
```

Use the complete cookie file, beginning with `# Netscape HTTP Cookie File`. The app normalizes CRLF/CR line endings and repairs wrapped rows before writing a private temporary Netscape file for yt-dlp. Optional PO-token configuration is `YTDLP_PO_TOKEN` with `YTDLP_PO_TOKEN_CLIENT` (default `mweb`). Tokens may be video/session-specific and must come from a maintained, trusted provider.

The cookie secret is written to a private temporary file only for the download step, passed to yt-dlp for its client attempts, then deleted. Never put cookie contents in source control, logs, or support messages. A cookie or PO token does not guarantee access from a Streamlit Cloud datacenter IP: if YouTube is blocking the hosting IP itself, extractor settings cannot reliably remove that restriction.
