// Copy for the shadow-fleet page (/[lang]/shadow-fleet/). Numbers are filled in the browser from
// shadow.json ({n}, {later}, {months}, ...), so the text stays true as more months are exported.

import type { Lang } from '../i18n/ui';

export interface ShadowText {
  pageTitle: string;
  description: string;
  kicker: string;
  title: string;
  dek: string;
  mapLegend: string;
  statSeen: string;
  statBefore: string;
  statLead: string;
  statLeadValue: string;
  statFlags: string;
  monthsTitle: string;
  monthsSub: string;
  monthsLater: string;
  monthsBefore: string;
  monthsObserved: string;
  monthsEarlier: string;
  monthsTip: string;
  flagsTitle: string;
  flagsSub: string;
  flagsOther: string;
  regimeTitle: string;
  regimeSub: string;
  regime: Record<'russia' | 'iran' | 'other', string>;
  sourceTitle: string;
  sourceSub: string;
  sourceUk: string;
  sourceUs: string;
  sourceBoth: string;
  sourceNote: string;
  tableTitle: string;
  placeholder: string;
  filterAll: string;
  filterLater: string;
  filterBefore: string;
  colVessel: string;
  colSeen: string;
  colSanctioned: string;
  colLead: string;
  colSignals: string;
  days: string;
  day: string;
  leadMonths: string;
  leadMonth: string;
  leadDays: string;
  leadAlready: string;
  alsoKnownAs: string;
  signals: Record<'gaps' | 'sts' | 'draught', string>;
  none: string;
  noResults: string;
  notesTitle: string;
  notes: string[];
}

export const SHADOW: Record<Lang, ShadowText> = {
  en: {
    pageTitle: 'The shadow fleet',
    description:
      'Every vessel on the US or UK sanctions lists that public AIS signals caught crossing the Danish straits: when it passed, under which flag, and when the sanctions came.',
    kicker: 'The shadow fleet',
    title: '{n} sanctioned ships crossed the Danish straits',
    dek: '{when}, public AIS signals recorded {n} vessels that are now on the US or UK sanctions lists. {later} of them sailed through before they were sanctioned.',
    mapLegend: 'Route of a sanctioned vessel',
    statSeen: 'sanctioned vessels seen',
    statBefore: 'passed before they were sanctioned',
    statLead: 'median time from passage to sanction',
    statLeadValue: '{n} months',
    statFlags: 'changed flag while we watched',
    monthsTitle: 'When they were sanctioned',
    monthsSub: 'Each vessel’s first designation, by month. Shaded: the months with AIS data.',
    monthsLater: 'Sanctioned after it passed',
    monthsBefore: 'Already sanctioned when it passed',
    monthsObserved: 'AIS data',
    monthsEarlier: 'before',
    monthsTip: '{n} vessels',
    flagsTitle: 'The flag they sailed under',
    flagsSub: 'Each vessel’s first flag seen in these months.',
    flagsOther: 'Others',
    regimeTitle: 'Why they were sanctioned',
    regimeSub: 'The programme of each vessel’s first designation.',
    regime: { russia: 'Russia', iran: 'Iran', other: 'Other programmes' },
    sourceTitle: 'Who sanctioned them',
    sourceSub: 'Vessels on each list.',
    sourceUk: 'UK only',
    sourceUs: 'US only',
    sourceBoth: 'Both',
    sourceNote:
      'The EU bars hundreds of these vessels from its ports (Annex XLII of Regulation 833/2014), but that list is not in the EU financial sanctions file this project uses, which holds only two vessels. That is why the EU barely appears here.',
    tableTitle: 'All {n}, one by one',
    placeholder: 'Name, IMO or flag…',
    filterAll: 'All',
    filterLater: 'Sanctioned later',
    filterBefore: 'Already sanctioned',
    colVessel: 'Vessel',
    colSeen: 'Seen',
    colSanctioned: 'Sanctioned',
    colLead: 'Lead',
    colSignals: 'Signals',
    days: '{n} days',
    day: '1 day',
    leadMonths: '{n} months before',
    leadMonth: '1 month before',
    leadDays: '{n} days before',
    leadAlready: 'already sanctioned',
    alsoKnownAs: 'also',
    signals: {
      gaps: 'AIS gaps',
      sts: 'ship-to-ship encounters',
      draught: 'draught changes with no port call',
    },
    none: '—',
    noResults: 'No vessel matches.',
    notesTitle: 'How to read this page',
    notes: [
      '“Sanctioned” means the IMO number the vessel broadcast is on the US (OFAC) or UK (OFSI) lists as of {snapshot}. The match is by IMO: a vessel broadcasting someone else’s IMO would appear under the wrong identity.',
      '“Seen” means at least one AIS position inside Danish coverage in the months built so far ({months}). The lead is measured from the first time we saw it in those months, not from its first real passage.',
      'Passing before a sanction does not mean anyone could have predicted it: most tankers that cross these straits are never sanctioned. This site publishes no predictions.',
    ],
  },
  es: {
    pageTitle: 'La flota en la sombra',
    description:
      'Todos los buques de las listas de sanciones de EE. UU. o Reino Unido que las señales AIS públicas vieron cruzar los estrechos daneses: cuándo pasaron, con qué bandera y cuándo llegó la sanción.',
    kicker: 'La flota en la sombra',
    title: '{n} buques sancionados cruzaron los estrechos daneses',
    dek: '{when}, las señales AIS públicas registraron {n} barcos que hoy están en las listas de sanciones de EE. UU. o Reino Unido. {later} de ellos pasaron antes de ser sancionados.',
    mapLegend: 'Ruta de un buque sancionado',
    statSeen: 'buques sancionados vistos',
    statBefore: 'pasaron antes de ser sancionados',
    statLead: 'mediana entre el paso y la sanción',
    statLeadValue: '{n} meses',
    statFlags: 'cambiaron de bandera mientras los veíamos',
    monthsTitle: 'Cuándo los sancionaron',
    monthsSub: 'Primera designación de cada buque, por mes. Sombreado: meses con datos AIS.',
    monthsLater: 'Sancionado después de pasar',
    monthsBefore: 'Ya sancionado al pasar',
    monthsObserved: 'datos AIS',
    monthsEarlier: 'antes',
    monthsTip: '{n} buques',
    flagsTitle: 'La bandera con la que pasaron',
    flagsSub: 'Primera bandera vista de cada buque en estos meses.',
    flagsOther: 'Otras',
    regimeTitle: 'Por qué los sancionaron',
    regimeSub: 'Programa de la primera sanción de cada buque.',
    regime: { russia: 'Rusia', iran: 'Irán', other: 'Otros programas' },
    sourceTitle: 'Quién los sancionó',
    sourceSub: 'Buques en cada lista.',
    sourceUk: 'Solo Reino Unido',
    sourceUs: 'Solo EE. UU.',
    sourceBoth: 'Ambos',
    sourceNote:
      'La UE prohíbe la entrada en sus puertos a cientos de estos buques (anexo XLII del Reglamento 833/2014), pero esa lista no está en el fichero de sanciones financieras de la UE que usa este proyecto, que solo recoge dos buques. Por eso la UE casi no aparece aquí.',
    tableTitle: 'Los {n}, uno a uno',
    placeholder: 'Nombre, IMO o bandera…',
    filterAll: 'Todos',
    filterLater: 'Sancionados después',
    filterBefore: 'Ya sancionados',
    colVessel: 'Buque',
    colSeen: 'Visto',
    colSanctioned: 'Sancionado',
    colLead: 'Antelación',
    colSignals: 'Señales',
    days: '{n} días',
    day: '1 día',
    leadMonths: '{n} meses antes',
    leadMonth: '1 mes antes',
    leadDays: '{n} días antes',
    leadAlready: 'ya sancionado',
    alsoKnownAs: 'también',
    signals: {
      gaps: 'apagones AIS',
      sts: 'encuentros entre buques',
      draught: 'cambios de calado sin escala',
    },
    none: '—',
    noResults: 'Ningún buque coincide.',
    notesTitle: 'Cómo leer esta página',
    notes: [
      '«Sancionado» significa que el número IMO que transmitía el buque figura en las listas de EE. UU. (OFAC) o Reino Unido (OFSI) a fecha de {snapshot}. El emparejamiento es por IMO: un barco que transmita un IMO ajeno aparecería con la identidad equivocada.',
      '«Visto» significa al menos una posición AIS en la cobertura danesa en los meses construidos hasta ahora ({months}). La antelación se mide desde la primera vez que lo vimos en esos meses, no desde su primer paso real.',
      'Que un barco pasara antes de ser sancionado no significa que se pudiera predecir: la mayoría de los petroleros que cruzan estos estrechos nunca son sancionados. Esta web no publica predicciones.',
    ],
  },
};
