/**
 * The front-page story, in both languages. Every fact about NS LOTUS below was read from this
 * project's own June 2024 export (its dossier and track) and the frozen 2026-09-21 sanctions
 * snapshot; the aggregate figures ({placeholders}) are computed in the browser from the same
 * files the map loads, so the text cannot drift from the data.
 */
import type { Lang } from '../i18n/ui';

export interface StoryStep {
  kicker?: string;
  title: string;
  body: string[];
}

export interface StoryText {
  heroKicker: string;
  heroTitle: string;
  heroDek: string;
  heroMeta: string;
  steps: StoryStep[];
  chartTitle: string;
  chartLater: string;
  chartOther: string;
  chartRows: { gap: string; spoof: string; sts: string; draught: string };
  chartNote: string;
  ctaTitle: string;
  ctaBody: string;
  labels: {
    lastContact: string;
    back: string;
    ballast: string;
    laden: string;
    beyond: string;
  };
}

export const CASE_MMSI = 626395000; // NS LOTUS, IMO 9339337

const en: StoryText = {
  heroKicker: 'An open-data investigation',
  heroTitle: 'The tanker that said where it was going',
  heroDek:
    'Russia’s oil leaves the Baltic through the Danish straits. Public AIS signals show how the tankers that were sanctioned later sailed through, and how little they tried to hide.',
  heroMeta: 'Danish straits · June 2024 · AIS from the Danish Maritime Authority · US, UK and EU sanctions lists',
  steps: [
    {
      title: 'A narrow door',
      body: [
        'Crude and products from Primorsk and Ust-Luga, Russia’s big Baltic export terminals, can only reach the ocean through the Danish straits.',
        'In June 2024 the Danish Maritime Authority’s receivers heard {vessels} different vessels here. Every glowing line on this map is where they sailed.',
      ],
    },
    {
      title: 'The shadow fleet',
      body: [
        'Since the full-scale invasion of Ukraine, much of Russia’s seaborne oil travels on ageing tankers with opaque owners and flags of convenience, outside the Western price cap.',
        'Of the {tankers} tankers heard in June, {later} were put on a US, UK or EU sanctions list afterwards. They are drawn in orange.',
      ],
    },
    {
      kicker: '17 June 2024',
      title: 'NS LOTUS comes in empty',
      body: [
        'A 249-metre tanker enters from the North Sea under the flag of Gabon. Its transponder broadcasts a destination typed by the crew: EGSUZ>RUPRI, Suez to Primorsk.',
        'Its declared draught, how deep the hull sits in the water, is 8.2 metres. It is riding high: in ballast, carrying no cargo.',
      ],
    },
    {
      kicker: '19 → 26 June',
      title: 'Six and a half days out of sight',
      body: [
        'East of Bornholm the signal fades from the Danish receivers for 158 hours. This is where the ship leaves their range; the Gulf of Finland, where Primorsk and Ust-Luga are, lies beyond it.',
        'What happened there is not in this data.',
      ],
    },
    {
      kicker: '26 June 2024',
      title: 'It comes back 5.8 metres deeper',
      body: [
        'It reappears at the same spot, now declaring RUULU>EGPSD, Ust-Luga to Port Said, with a draught of 14.0 metres. It is laden.',
        'A change that large with no port call in sight is exactly what one of this project’s five detectors looks for. It flagged NS LOTUS for this crossing.',
      ],
    },
    {
      kicker: '31 July 2024',
      title: 'Sanctioned five weeks later',
      body: [
        'The United Kingdom designated NS LOTUS under its Russia sanctions regulations.',
        'When the United States followed on 10 January 2025, the ship was listed as LEGACY, under the flag of Barbados. New name, new flag, same IMO number: that number stays with a hull for life, which is why this project follows vessels by it.',
      ],
    },
    {
      title: 'They don’t hide here',
      body: [
        'You might expect sanctioned tankers to switch off their transponders or fake their positions. In Danish waters they mostly don’t: compared with other tankers heard in June, those sanctioned later went silent less often, rarely produced impossible positions, and were never seen in a ship-to-ship transfer.',
        'What stands out is the draught. More of them changed it with no port call we could see: the loading happens beyond the horizon.',
      ],
    },
  ],
  chartTitle: 'Tankers heard in June 2024 with at least one detector event',
  chartLater: 'Sanctioned later ({n})',
  chartOther: 'Other tankers ({n})',
  chartRows: {
    gap: 'AIS gap',
    spoof: 'Position anomaly',
    sts: 'Ship-to-ship encounter',
    draught: 'Draught change with no port call',
  },
  chartNote:
    'Signals, not accusations: declaring a destination or changing draught is legal. Sanctions lists catch up with vessels for reasons this data cannot see, such as ownership, insurance and where the cargo came from.',
  ctaTitle: 'See it for yourself',
  ctaBody: 'Replay a whole month of traffic, open any vessel’s dossier, or watch the straits live.',
  labels: {
    lastContact: 'Last heard 19 Jun 18:31',
    back: 'Heard again 26 Jun 08:46',
    ballast: '8.2 m · empty',
    laden: '14.0 m · laden',
    beyond: 'Beyond Danish receivers',
  },
};

const es: StoryText = {
  heroKicker: 'Una investigación con datos abiertos',
  heroTitle: 'El petrolero que dijo adónde iba',
  heroDek:
    'El petróleo ruso sale del Báltico por los estrechos daneses. Las señales AIS públicas muestran cómo los cruzaron los petroleros que después fueron sancionados, y lo poco que intentaron esconderse.',
  heroMeta: 'Estrechos daneses · junio de 2024 · AIS de la Autoridad Marítima de Dinamarca · listas de sanciones de EE. UU., Reino Unido y UE',
  steps: [
    {
      title: 'Una puerta estrecha',
      body: [
        'El crudo y los productos de Primorsk y Ust-Luga, las grandes terminales rusas de exportación del Báltico, solo pueden llegar al océano atravesando los estrechos daneses.',
        'En junio de 2024 los receptores de la Autoridad Marítima de Dinamarca escucharon aquí a {vessels} buques distintos. Cada línea luminosa del mapa es por donde navegaron.',
      ],
    },
    {
      title: 'La flota en la sombra',
      body: [
        'Desde la invasión a gran escala de Ucrania, buena parte del petróleo ruso viaja en petroleros envejecidos, con propietarios opacos y banderas de conveniencia, al margen del tope de precio occidental.',
        'De los {tankers} petroleros escuchados en junio, {later} acabaron después en una lista de sanciones de EE. UU., Reino Unido o la UE. Están dibujados en naranja.',
      ],
    },
    {
      kicker: '17 de junio de 2024',
      title: 'El NS LOTUS entra vacío',
      body: [
        'Un petrolero de 249 metros entra desde el mar del Norte con bandera de Gabón. Su transpondedor emite un destino que teclea la tripulación: EGSUZ>RUPRI, de Suez a Primorsk.',
        'Su calado declarado, lo que el casco se hunde en el agua, es de 8,2 metros. Va alto: en lastre, sin carga.',
      ],
    },
    {
      kicker: '19 → 26 de junio',
      title: 'Seis días y medio sin rastro',
      body: [
        'Al este de Bornholm la señal se pierde para los receptores daneses durante 158 horas. Ahí el buque sale de su alcance; el golfo de Finlandia, donde están Primorsk y Ust-Luga, queda más allá.',
        'Lo que pasó allí no está en estos datos.',
      ],
    },
    {
      kicker: '26 de junio de 2024',
      title: 'Vuelve 5,8 metros más hundido',
      body: [
        'Reaparece en el mismo punto, ahora declarando RUULU>EGPSD, de Ust-Luga a Port Said, con un calado de 14,0 metros. Va cargado.',
        'Un cambio así sin ninguna escala a la vista es justo lo que busca uno de los cinco detectores de este proyecto. Marcó al NS LOTUS en esta travesía.',
      ],
    },
    {
      kicker: '31 de julio de 2024',
      title: 'Sancionado cinco semanas después',
      body: [
        'El Reino Unido designó al NS LOTUS bajo su normativa de sanciones a Rusia.',
        'Cuando Estados Unidos lo sancionó el 10 de enero de 2025, el buque figuraba como LEGACY y con bandera de Barbados. Nuevo nombre, nueva bandera, mismo número IMO: ese número acompaña al casco toda su vida, y por eso este proyecto sigue a los buques por él.',
      ],
    },
    {
      title: 'Aquí no se esconden',
      body: [
        'Cabría esperar que los petroleros sancionados apagaran el transpondedor o falsearan su posición. En aguas danesas, en general, no lo hacen: frente al resto de petroleros de junio, los sancionados después se quedaron en silencio menos a menudo, casi nunca dieron posiciones imposibles y ninguno apareció en un trasvase entre buques.',
        'Lo que destaca es el calado. Más de ellos lo cambiaron sin ninguna escala a la vista: la carga ocurre más allá del horizonte.',
      ],
    },
  ],
  chartTitle: 'Petroleros escuchados en junio de 2024 con al menos un evento',
  chartLater: 'Sancionados después ({n})',
  chartOther: 'Resto de petroleros ({n})',
  chartRows: {
    gap: 'Apagón AIS',
    spoof: 'Anomalía de posición',
    sts: 'Encuentro entre buques',
    draught: 'Cambio de calado sin escala',
  },
  chartNote:
    'Señales, no acusaciones: declarar un destino o cambiar de calado es legal. Las listas de sanciones alcanzan a los buques por motivos que estos datos no ven, como la propiedad, el seguro o el origen de la carga.',
  ctaTitle: 'Compruébalo tú',
  ctaBody: 'Reproduce un mes entero de tráfico, abre la ficha de cualquier buque o mira los estrechos en directo.',
  labels: {
    lastContact: 'Última señal 19 jun 18:31',
    back: 'Vuelve a oírse 26 jun 08:46',
    ballast: '8,2 m · vacío',
    laden: '14,0 m · cargado',
    beyond: 'Fuera del alcance danés',
  },
};

export const STORY: Record<Lang, StoryText> = { en, es };
