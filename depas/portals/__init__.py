from types import ModuleType

from depas.portals import chilepropiedades, goplaceit, houm, portalinmobiliario, toctoc

# Each portal module exposes NAME, search(fetcher, query) and fetch_detail(fetcher, url).
PORTALS: dict[str, ModuleType] = {
    module.NAME: module
    for module in (portalinmobiliario, houm, goplaceit, toctoc, chilepropiedades)
}
