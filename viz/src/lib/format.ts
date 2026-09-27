import { LOCALE, t, type Lang, type UIKey } from '../i18n/ui';

const regionNames = new Map<Lang, Intl.DisplayNames>();

export function countryName(lang: Lang, iso2: string | null | undefined): string | null {
  if (!iso2) return null;
  if (!regionNames.has(lang)) {
    regionNames.set(lang, new Intl.DisplayNames([LOCALE[lang]], { type: 'region' }));
  }
  try {
    return regionNames.get(lang)!.of(iso2) ?? iso2;
  } catch {
    return iso2;
  }
}

export function flagClass(iso2: string | null | undefined): string {
  return iso2 ? `flag fi fi-${iso2.toLowerCase()}` : 'flag flag-unknown';
}

export function shipTypeLabel(lang: Lang, type: string | null | undefined): string {
  if (!type) return t(lang, 'type.Undefined');
  const key = `type.${type}` as UIKey;
  const label = t(lang, key);
  return label === key ? type : label;
}

export function kindLabel(lang: Lang, kind: string): string {
  const key = `kind.${kind}` as UIKey;
  const label = t(lang, key);
  return label === key ? kind.replaceAll('_', ' ') : label;
}

export function num(lang: Lang, value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return new Intl.NumberFormat(LOCALE[lang], {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  }).format(value);
}

function toDate(value: string | Date | number): Date {
  if (value instanceof Date) return value;
  if (typeof value === 'number') return new Date(value);
  // Exported timestamps are UTC: "2024-06-12T14:30Z" or a bare date "2024-06-12".
  return new Date(value.length === 10 ? `${value}T00:00:00Z` : value.replace(/Z?$/, 'Z'));
}

export function fmtDate(lang: Lang, value: string | Date | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return new Intl.DateTimeFormat(LOCALE[lang], {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(toDate(value));
}

export function fmtDateTime(lang: Lang, value: string | Date | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return new Intl.DateTimeFormat(LOCALE[lang], {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    timeZone: 'UTC',
  }).format(toDate(value));
}

export function fmtMonth(lang: Lang, value: string): string {
  const s = new Intl.DateTimeFormat(LOCALE[lang], {
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(toDate(value));
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function fmtClock(lang: Lang, date: Date): { day: string; time: string } {
  return {
    day: new Intl.DateTimeFormat(LOCALE[lang], {
      weekday: 'short',
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      timeZone: 'UTC',
    }).format(date),
    time: new Intl.DateTimeFormat(LOCALE[lang], {
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
      timeZone: 'UTC',
    }).format(date),
  };
}

/** Whole months between two dates (b after a), for "sanctioned N months later". */
export function monthsBetween(a: string | Date, b: string | Date): number {
  const da = toDate(a as string);
  const db = toDate(b as string);
  return (db.getUTCFullYear() - da.getUTCFullYear()) * 12 + (db.getUTCMonth() - da.getUTCMonth());
}

export function monthsLabel(lang: Lang, n: number): string {
  return n === 1 ? t(lang, 'vessel.month') : t(lang, 'vessel.months', { n });
}

/** Vessel names are shown as broadcast (AIS names are upper case, and title-casing them breaks
 * acronyms such as SCF or NS); this only trims and collapses whitespace. */
export function title(value: string | null | undefined): string {
  if (!value) return '';
  return value.trim().replace(/\s+/g, ' ');
}

const SOURCES: Record<string, Record<Lang, string>> = {
  ofac: { en: 'US · OFAC', es: 'EE. UU. · OFAC' },
  uk: { en: 'UK · OFSI', es: 'Reino Unido · OFSI' },
  eu: { en: 'EU', es: 'UE' },
};

export function sourceLabel(lang: Lang, source: string): string {
  return SOURCES[source]?.[lang] ?? source.toUpperCase();
}
