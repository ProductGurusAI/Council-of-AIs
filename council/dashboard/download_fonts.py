import os
import urllib.request

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
os.makedirs(FONTS_DIR, exist_ok=True)

# Pinned unpkg CDN URLs (npm packages @fontsource/fira-sans and firacode)
FONT_URLS = {
    "FiraCode-Regular.woff2": "https://unpkg.com/firacode@6.2.0/distr/woff2/FiraCode-Regular.woff2",
    "FiraCode-Medium.woff2": "https://unpkg.com/firacode@6.2.0/distr/woff2/FiraCode-Medium.woff2",
    "FiraCode-Bold.woff2": "https://unpkg.com/firacode@6.2.0/distr/woff2/FiraCode-Bold.woff2",
    "FiraSans-Regular.woff2": "https://unpkg.com/@fontsource/fira-sans@5.0.17/files/fira-sans-latin-400-normal.woff2",
    "FiraSans-Medium.woff2": "https://unpkg.com/@fontsource/fira-sans@5.0.17/files/fira-sans-latin-500-normal.woff2",
    "FiraSans-Bold.woff2": "https://unpkg.com/@fontsource/fira-sans@5.0.17/files/fira-sans-latin-700-normal.woff2",
    "FiraSans-Light.woff2": "https://unpkg.com/@fontsource/fira-sans@5.0.17/files/fira-sans-latin-300-normal.woff2"
}

headers = {"User-Agent": "Mozilla/5.0"}

print("Downloading Fira fonts from unpkg...")
for name, url in FONT_URLS.items():
    dest = os.path.join(FONTS_DIR, name)
    if os.path.exists(dest):
        print(f"Skipping {name} (already downloaded)")
        continue
    try:
        print(f"Downloading {name}...")
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response:
            with open(dest, "wb") as f:
                f.write(response.read())
        print(f"Successfully downloaded {name}")
    except Exception as e:
        print(f"Failed to download {name}: {e}")
