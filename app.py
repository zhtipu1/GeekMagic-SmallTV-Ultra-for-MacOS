import webview
import os
from api import GeekMagicAPI

UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
INDEX = os.path.join(UI_DIR, "index.html")


def main():
    api = GeekMagicAPI()
    window = webview.create_window(
        title="GeekMagic SmallTV-Ultra",
        url=INDEX,
        js_api=api,
        width=1060,
        height=720,
        min_size=(860, 600),
        background_color="#0d1117",
        text_select=False,
    )
    # Dock icon comes from the .app bundle's Info.plist (set via py2app), not from here.
    webview.start(debug=False)


if __name__ == "__main__":
    main()
