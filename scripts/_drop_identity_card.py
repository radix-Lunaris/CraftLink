"""One-off: remove the _build_identity_card method from app.py."""
import io

PATH = "client/mclink/gui/app.py"

src = io.open(PATH, encoding="utf-8").read()

start = src.index("    def _build_identity_card(self, parent):")
end = src.index("    def _build_server_card(self, parent):")

removed = src[start:end]
src = src[:start] + src[end:]

io.open(PATH, "w", encoding="utf-8").write(src)

print("removed lines:", len(removed.splitlines()))
print("still referenced:", src.count("_build_identity_card"))
print("username_var refs:", src.count("username_var"))
print("client_id_var refs:", src.count("client_id_var"))
