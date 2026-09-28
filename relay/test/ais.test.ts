// Mirrors tests/test_aisstream.py so the Worker relay and the Python relay parse AISStream the
// same way. Synthetic messages in AISStream's JSON shape; no network, no API key.
// Run: npm test (Node >= 23 strips the types itself).

import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  STALE_AFTER_S,
  applyMessage,
  cleanText,
  compact,
  flagOf,
  prune,
  regionOf,
  shipCategory,
  subscription,
  type Vessel,
} from '../src/ais.ts';

function position(opts: { mmsi?: number; lat?: number; lon?: number; kind?: string; [k: string]: unknown } = {}) {
  const { mmsi = 219000001, lat = 55.5, lon = 11.0, kind = 'PositionReport', ...extra } = opts;
  const body = { UserID: mmsi, Latitude: lat, Longitude: lon, Sog: 11.2, Cog: 45.0, TrueHeading: 44, NavigationalStatus: 0, ...extra };
  return { MessageType: kind, Message: { [kind]: body }, MetaData: { MMSI: mmsi, ShipName: 'TEST SHIP@@@' } };
}

function staticData(extra: Record<string, unknown> = {}) {
  const body = {
    UserID: 219000001, Name: 'NORDIC  STAR@@@@', CallSign: 'OXAB2@', ImoNumber: 9321483, Type: 84,
    Destination: 'PRIMORSK@@@@', MaximumStaticDraught: 14.2, Dimension: { A: 200, B: 50, C: 22, D: 22 },
    Eta: { Month: 7, Day: 3, Hour: 14, Minute: 30 }, ...extra,
  };
  return { MessageType: 'ShipStaticData', Message: { ShipStaticData: body }, MetaData: {} };
}

test('cleanText strips AIS padding', () => {
  assert.equal(cleanText('NORDIC  STAR@@@@'), 'NORDIC STAR');
  assert.equal(cleanText('@@@@'), null);
  assert.equal(cleanText(null), null);
});

test('shipCategory', () => {
  assert.equal(shipCategory(84), 'Tanker');
  assert.equal(shipCategory(70), 'Cargo');
  assert.equal(shipCategory(37), 'Pleasure');
  assert.equal(shipCategory(52), 'Tug');
  assert.equal(shipCategory(99), 'Other');
  assert.equal(shipCategory(0), null);
});

test('regionOf', () => {
  assert.equal(regionOf(55.5, 11.0), 'dk');
  assert.equal(regionOf(36.0, -5.5), 'gib');
  assert.equal(regionOf(0.0, 0.0), null);
});

test('subscription uses [lat, lon] corners and every region', () => {
  const sub = subscription('k', ['dk', 'gib']);
  assert.equal(sub.APIKey, 'k');
  assert.deepEqual(sub.BoundingBoxes[0], [[53.3, 2.0], [59.3, 19.0]]);
  assert.equal(sub.BoundingBoxes.length, 2);
  assert.ok(sub.FilterMessageTypes.includes('ShipStaticData'));
});

test('flagOf maps the MID of ship stations only', () => {
  assert.equal(flagOf(219000001), 'DK');
  assert.equal(flagOf(461000233), 'OM');
  assert.equal(flagOf(111219500), null); // SAR aircraft, not a ship station
});

test('a position then static data build one record', () => {
  const vessels = new Map<number, Vessel>();
  assert.equal(applyMessage(vessels, position(), 1000), 219000001);
  const v = vessels.get(219000001)!;
  assert.deepEqual([v.la, v.lo, v.s, v.c, v.h, v.n, v.r], [55.5, 11.0, 11.2, 45.0, 44, 0, 'dk']);
  assert.equal(v.f, 'DK');
  assert.equal(v.nm, 'TEST SHIP'); // from MetaData until static data arrives
  applyMessage(vessels, staticData(), 1001);
  assert.equal(v.nm, 'NORDIC STAR');
  assert.deepEqual([v.imo, v.ty, v.d, v.dr, v.L, v.W, v.eta], ['9321483', 'Tanker', 'PRIMORSK', 14.2, 250, 44, '07-03 14:30']);
});

test('"not available" sentinels become null', () => {
  const vessels = new Map<number, Vessel>();
  applyMessage(vessels, position({ Sog: 102.3, Cog: 360.0, TrueHeading: 511, NavigationalStatus: 15 }), 1);
  const v = vessels.get(219000001)!;
  assert.deepEqual([v.s, v.c, v.h, v.n], [null, null, null, null]);
});

test('positions outside the regions, or invalid, are ignored', () => {
  const vessels = new Map<number, Vessel>();
  assert.equal(applyMessage(vessels, position({ lat: 10.0, lon: 10.0 }), 1), null);
  assert.equal(applyMessage(vessels, position({ lat: 91.0, lon: 181.0 }), 1), null);
  assert.equal(applyMessage(vessels, { MessageType: 'Unknown', Message: {} }, 1), null);
});

test('an invalid IMO is not kept', () => {
  const vessels = new Map<number, Vessel>();
  applyMessage(vessels, position(), 1);
  applyMessage(vessels, staticData({ ImoNumber: 0 }), 2);
  assert.equal(vessels.get(219000001)!.imo ?? null, null);
});

test('class B static report', () => {
  const vessels = new Map<number, Vessel>();
  applyMessage(vessels, position({ kind: 'StandardClassBPositionReport' }), 1);
  const msg = {
    MessageType: 'StaticDataReport',
    Message: {
      StaticDataReport: {
        UserID: 219000001,
        ReportA: { Valid: true, Name: 'SEA BREEZE@@' },
        ReportB: { Valid: true, CallSign: 'OU1234', ShipType: 37, Dimension: { A: 8, B: 4, C: 2, D: 2 } },
      },
    },
  };
  applyMessage(vessels, msg, 2);
  const v = vessels.get(219000001)!;
  assert.deepEqual([v.k, v.nm, v.cs, v.ty, v.L], ['B', 'SEA BREEZE', 'OU1234', 'Pleasure', 12]);
});

test('prune drops stale vessels', () => {
  const vessels = new Map<number, Vessel>();
  applyMessage(vessels, position({ mmsi: 219000001 }), 0);
  applyMessage(vessels, position({ mmsi: 219000002 }), STALE_AFTER_S - 10);
  assert.deepEqual(prune(vessels, STALE_AFTER_S + 1), [219000001]);
  assert.deepEqual([...vessels.keys()], [219000002]);
});

test('compact drops empty fields', () => {
  assert.deepEqual(compact({ m: 1, nm: null, s: 0.0 }), { m: 1, s: 0.0 });
});
