"""CI-only check: prove the Personal AI feature's dependencies actually
import from inside a frozen bundle, not just from the source tree.

`anthropic` and `keyring.backends.Windows` are declared as PyInstaller
hiddenimports (see MusicStudio.spec) because static analysis can't discover
them on its own. Declaring a hiddenimport is not the same as proving it
resolves once frozen -- this script is built into its own tiny PyInstaller
executable in CI and run standalone to check exactly that.

Not part of the shipped app; nothing here is imported by musicstudio itself.
"""

import anthropic
import keyring

from musicstudio.core import assistant, secrets

print("anthropic", anthropic.__version__)
print("keyring", keyring.get_keyring())
print("assistant tools", sorted(assistant.build_tools()))
print("secrets.keyring_available", secrets.keyring_available())
print("FROZEN_AI_IMPORTS_OK")
