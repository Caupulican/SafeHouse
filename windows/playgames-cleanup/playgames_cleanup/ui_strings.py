"""Every UI string the automation looks for, in one place.

The Play Games client is localized, and this PC runs it in pt-BR, so each control is
looked up by a TUPLE of accessible names - English first, Portuguese after. Matching is
case-insensitive and whitespace-trimmed (see `app.norm`). When the first live run shows
a name that differs from what is here, this is the only file to edit.

Nothing in this module was read off the live client: it is what the UI is EXPECTED to
call things. The README lists which of these must be confirmed on the first run.
"""

from __future__ import annotations

# --- the app -----------------------------------------------------------------

# Same directory the guard kills under (playgames_guard.KILL_ROOT), trailing backslash
# included: it is the identity test for "this window belongs to Play Games", and it keeps
# the sibling `Play Games Services` directory out structurally.
INSTALL_ROOT = "C:\\Program Files\\Google\\Play Games\\"
BOOTSTRAPPER = INSTALL_ROOT + "Bootstrapper.exe"

# Top-level window titles. The beta channel has carried a " beta" suffix.
MAIN_WINDOW_TITLES = ("Google Play Games", "Google Play Games beta",
                      "Google Play Games (beta)")

# --- navigation --------------------------------------------------------------

LIBRARY = ("Library", "Biblioteca")
# The filter chip that narrows the library to what is on disk. Optional: if it is not
# found the whole library is enumerated and the per-game prompt sorts it out.
INSTALLED_FILTER = ("Installed", "Instalados", "Instalado")

# --- per-game controls -------------------------------------------------------

# The per-tile overflow button. Chromium usually exposes it either bare or with the game
# name folded in ("More options for X"), so both exact names and prefixes are listed.
MORE_OPTIONS = ("More options", "More", "Mais opções", "Mais opcoes", "Mais",
                "Opções", "Options")
MORE_OPTIONS_PREFIXES = ("more options for ", "mais opções de ", "mais opções para ",
                         "opções de ", "options for ")

# The menu item, and the confirm button in the dialog that (probably) follows it. They
# share a name in both languages, which is why the confirm step insists on exactly one
# match and refuses to guess between two.
UNINSTALL = ("Uninstall", "Desinstalar")
CONFIRM = UNINSTALL + ("OK", "Confirm", "Confirmar", "Remove", "Remover")
# Never pressed by the tool; listed so a candidate list can exclude them explicitly.
CANCEL = ("Cancel", "Cancelar", "Close", "Fechar", "Back", "Voltar")

# --- enumeration -------------------------------------------------------------

# Control types a library tile may surface as, in the order they are searched.
TILE_CONTROL_TYPES = ("ListItem", "Hyperlink", "Button", "Group", "Image")

# Accessible names that are app chrome, never a game. Case-insensitive. Generous on
# purpose: a false entry here only means one more "[s]kip" for the parent, but a missing
# one cannot cause harm either - nothing is uninstalled without an explicit "y".
CHROME_NAMES = frozenset(n.casefold() for n in (
    # navigation
    "Home", "Início", "Inicio", "Library", "Biblioteca", "Search", "Pesquisar",
    "Buscar", "Settings", "Configurações", "Configuracoes", "Profile", "Perfil",
    "Achievements", "Conquistas", "Notifications", "Notificações", "Help", "Ajuda",
    "Menu", "Back", "Voltar", "Forward", "Avançar", "Account", "Conta",
    "Google Play Games", "Google Play Games beta", "Google Play",
    # library filters / sort
    "Installed", "Instalados", "Instalado", "All", "Todos", "Tudo",
    "Sort", "Ordenar", "Filter", "Filtrar", "Recent", "Recentes",
    "Name", "Nome", "A-Z", "Z-A",
    # window caption
    "Minimize", "Minimizar", "Maximize", "Maximizar", "Restore", "Restaurar",
    "Close", "Fechar", "System", "Sistema",
    # verbs on tiles
    "Play", "Jogar", "Install", "Instalar", "Update", "Atualizar",
    "Uninstall", "Desinstalar", "Open", "Abrir",
) + MORE_OPTIONS + CANCEL)

# Longer than this is a sentence (a description, a banner), not a game title.
MAX_GAME_NAME_LEN = 60
