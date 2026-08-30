from enum import StrEnum


class Commune(StrEnum):
    """Communes of the Región Metropolitana that Portal Inmobiliario indexes.

    Nine RM communes are omitted because the portal has no page for them: Pirque, Tiltil,
    Calera de Tango, Paine, María Pinto, San Pedro, El Monte, Isla de Maipo and Peñaflor.
    """

    # Provincia de Santiago
    CERRILLOS = "cerrillos"
    CERRO_NAVIA = "cerro-navia"
    CONCHALI = "conchali"
    EL_BOSQUE = "el-bosque"
    ESTACION_CENTRAL = "estacion-central"
    HUECHURABA = "huechuraba"
    INDEPENDENCIA = "independencia"
    LA_CISTERNA = "la-cisterna"
    LA_FLORIDA = "la-florida"
    LA_GRANJA = "la-granja"
    LA_PINTANA = "la-pintana"
    LA_REINA = "la-reina"
    LAS_CONDES = "las-condes"
    LO_BARNECHEA = "lo-barnechea"
    LO_ESPEJO = "lo-espejo"
    LO_PRADO = "lo-prado"
    MACUL = "macul"
    MAIPU = "maipu"
    NUNOA = "nunoa"
    PEDRO_AGUIRRE_CERDA = "pedro-aguirre-cerda"
    PENALOLEN = "penalolen"
    PROVIDENCIA = "providencia"
    PUDAHUEL = "pudahuel"
    QUILICURA = "quilicura"
    QUINTA_NORMAL = "quinta-normal"
    RECOLETA = "recoleta"
    RENCA = "renca"
    SAN_JOAQUIN = "san-joaquin"
    SAN_MIGUEL = "san-miguel"
    SAN_RAMON = "san-ramon"
    SANTIAGO = "santiago"
    VITACURA = "vitacura"

    # Provincia Cordillera
    PUENTE_ALTO = "puente-alto"
    SAN_JOSE_DE_MAIPO = "san-jose-de-maipo"

    # Provincia de Chacabuco
    COLINA = "colina"
    LAMPA = "lampa"

    # Provincia de Maipo
    BUIN = "buin"
    SAN_BERNARDO = "san-bernardo"

    # Provincia de Melipilla
    ALHUE = "alhue"
    CURACAVI = "curacavi"
    MELIPILLA = "melipilla"

    # Provincia de Talagante
    PADRE_HURTADO = "padre-hurtado"
    TALAGANTE = "talagante"


SANTIAGO_PROVINCE = frozenset(
    {
        Commune.CERRILLOS, Commune.CERRO_NAVIA, Commune.CONCHALI, Commune.EL_BOSQUE,
        Commune.ESTACION_CENTRAL, Commune.HUECHURABA, Commune.INDEPENDENCIA, Commune.LA_CISTERNA,
        Commune.LA_FLORIDA, Commune.LA_GRANJA, Commune.LA_PINTANA, Commune.LA_REINA,
        Commune.LAS_CONDES, Commune.LO_BARNECHEA, Commune.LO_ESPEJO, Commune.LO_PRADO,
        Commune.MACUL, Commune.MAIPU, Commune.NUNOA, Commune.PEDRO_AGUIRRE_CERDA,
        Commune.PENALOLEN, Commune.PROVIDENCIA, Commune.PUDAHUEL, Commune.QUILICURA,
        Commune.QUINTA_NORMAL, Commune.RECOLETA, Commune.RENCA, Commune.SAN_JOAQUIN,
        Commune.SAN_MIGUEL, Commune.SAN_RAMON, Commune.SANTIAGO, Commune.VITACURA,
    }
)
