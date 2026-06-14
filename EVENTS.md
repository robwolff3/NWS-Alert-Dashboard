# NWS alert event reference

This is the full catalog of alert events the dashboard understands, generated
from the mapping tables in [`scripts/config.py`](scripts/config.py). It exists
so you can decide what to put in `FILTER_EVENT_CODES` and the
`NOTIFY_PRIORITY_*_CODES` lists in your `.env`.

## How events are coded

Every alert is tagged with a 3-letter **event code** (EEE):

- **Standard SAME/EAS codes** (49 below) are the official codes the
  National Weather Service broadcasts over NOAA Weather Radio
  ([weather.gov/nwr/eventcodes](https://www.weather.gov/nwr/eventcodes)). These
  can arrive from *any* source — radio (SAME decode), the API, or NWWS-OI.
- **Internal pseudo-codes** (71 below) cover common products that
  have **no** official SAME code, so they never come over the radio — only from
  the api.weather.gov polling source or NWWS-OI. The dashboard assigns each a
  stable `phen+sig` pseudo-code (e.g. `FGY` for a Dense Fog Advisory) so it
  routes through the same filter and priority machinery as a real EAS event.

The **VTEC** column shows the `phenomenon.significance` pair(s) that map to each
code. A blank VTEC means the product is identified by event name only (it
carries no VTEC string). Two pseudo-codes are deliberately *not* `phen+sig`
because that would collide with a real code: Lake Effect Snow uses `LK*`
(`LEW` is Law Enforcement Warning) and Lakeshore Flood uses `LF*` (`LSW` is
Land Slide Warning in the radio decoder's extended set).

## What to do with these

`FILTER_EVENT_CODES` is an **opt-in allowlist**:

- **Blank (the default) = accept everything.** Every event below notifies.
- **Non-blank = only the listed codes notify.** Any event *not* in your list is
  still ingested and shown on the dashboard, but stays silent (no push, no
  Apprise, no MQTT). An unmapped/informational product (one with no EEE at all)
  is likewise treated as filtered whenever a non-blank list is set.

You usually do **not** need to prune by geography. api.weather.gov only returns
alerts for the **zones and county you configured** (`FILTER_ZONES` /
`FILTER_SAME_CODES`, both auto-derived from `LOCATION`). An inland location
simply never receives a Small Craft Advisory or Storm Surge Warning, so leaving
the marine and tropical codes in your allowlist costs nothing. Trim
`FILTER_EVENT_CODES` only to silence event *types* you don't care about (for
example, dropping the advisory-tier codes if you only want warnings).

Priority routing is independent: `NOTIFY_PRIORITY_{5,4,3}_CODES` map a code to
a notification priority (which ntfy turns into an Android channel). A code in
none of those lists falls back to `NTFY_PRIORITY_DEFAULT`. See
[`.env.example`](.env.example) for the shipped defaults.

## Standard SAME / EAS event codes

These have an official SAME code and can arrive from radio, API, or NWWS.

| Code | Event name(s) | VTEC |
|---|---|---|
| `AVA` | Avalanche Watch | — |
| `AVW` | Avalanche Warning | — |
| `BLU` | Blue Alert | — |
| `BZW` | Blizzard Warning | BZ.W |
| `CAE` | Child Abduction Emergency | — |
| `CDW` | Civil Danger Warning | — |
| `CEM` | Civil Emergency Message | — |
| `CFA` | Coastal Flood Watch | CF.A |
| `CFW` | Coastal Flood Warning | CF.W |
| `DSW` | Dust Storm Warning | DS.W |
| `EQW` | Earthquake Warning | — |
| `EVI` | Evacuation - Immediate / Evacuation Immediate | — |
| `EWW` | Extreme Wind Warning | EW.W |
| `FFA` | Flash Flood Watch | FF.A |
| `FFS` | Flash Flood Statement | — |
| `FFW` | Flash Flood Warning | FF.W |
| `FLA` | Flood Watch | FA.A, FL.A |
| `FLS` | Flood Advisory / Flood Statement | FA.Y, FL.Y |
| `FLW` | Flood Warning | FA.W, FL.W |
| `FRW` | Fire Warning | — |
| `HLS` | Hurricane Local Statement | — |
| `HMW` | Hazardous Materials Warning | — |
| `HUA` | Hurricane Watch | HU.A |
| `HUW` | Hurricane Warning | HU.W |
| `HWA` | High Wind Watch | HW.A |
| `HWW` | High Wind Warning | HW.W |
| `LAE` | Local Area Emergency | — |
| `LEW` | Law Enforcement Warning | — |
| `NUW` | Nuclear Power Plant Warning | — |
| `RHW` | Radiological Hazard Warning | — |
| `SMW` | Special Marine Warning | MA.W |
| `SPS` | Special Weather Statement | — |
| `SPW` | Shelter In Place Warning | — |
| `SQW` | Snow Squall Warning | SQ.W |
| `SSA` | Storm Surge Watch | SS.A |
| `SSW` | Storm Surge Warning | SS.W |
| `SVA` | Severe Thunderstorm Watch | SV.A |
| `SVR` | Severe Thunderstorm Warning | SV.W |
| `SVS` | Severe Weather Statement | — |
| `TOA` | Tornado Watch | TO.A |
| `TOE` | 911 Telephone Outage Emergency | — |
| `TOR` | Tornado Warning | TO.W |
| `TRA` | Tropical Storm Watch | TR.A |
| `TRW` | Tropical Storm Warning | TR.W |
| `TSA` | Tsunami Watch | TS.A |
| `TSW` | Tsunami Warning | TS.W |
| `VOW` | Volcano Warning | — |
| `WSA` | Winter Storm Watch | WS.A |
| `WSW` | Winter Storm Warning | WS.W |

## Internal pseudo-codes (API / NWWS only)

These have no official SAME code — they only arrive from the API or NWWS-OI.

| Code | Event name(s) | VTEC |
|---|---|---|
| `AFW` | Ashfall Warning | AF.W |
| `AFY` | Ashfall Advisory | AF.Y |
| `AQA` | Air Quality Alert | — |
| `ASY` | Air Stagnation Advisory | AS.Y |
| `BHS` | Beach Hazards Statement | BH.S |
| `BSY` | Blowing Snow Advisory | BS.Y |
| `BWY` | Brisk Wind Advisory | BW.Y |
| `BZA` | Blizzard Watch | BZ.A |
| `CFS` | Coastal Flood Statement | CF.S |
| `CFY` | Coastal Flood Advisory | CF.Y |
| `CWY` | Cold Weather Advisory | CW.Y |
| `DUW` | Blowing Dust Warning | DU.W |
| `DUY` | Blowing Dust Advisory / Dust Advisory | DU.Y |
| `ECA` | Extreme Cold Watch | EC.A |
| `ECW` | Extreme Cold Warning | EC.W |
| `EHA` | Excessive Heat Watch | EH.A |
| `EHW` | Excessive Heat Warning | EH.W |
| `FGY` | Dense Fog Advisory | FG.Y |
| `FRY` | Frost Advisory | FR.Y |
| `FWA` | Fire Weather Watch | FW.A |
| `FZA` | Freeze Watch | FZ.A |
| `FZW` | Freeze Warning | FZ.W |
| `GLA` | Gale Watch | GL.A |
| `GLW` | Gale Warning | GL.W |
| `HFA` | Hurricane Force Wind Watch | HF.A |
| `HFW` | Hurricane Force Wind Warning | HF.W |
| `HTY` | Heat Advisory | HT.Y |
| `HYY` | Hydrologic Advisory | HY.Y |
| `HZA` | Hard Freeze Watch | HZ.A |
| `HZW` | Hard Freeze Warning | HZ.W |
| `ISW` | Ice Storm Warning | IS.W |
| `LFA` | Lakeshore Flood Watch | LS.A |
| `LFS` | Lakeshore Flood Statement | LS.S |
| `LFW` | Lakeshore Flood Warning | LS.W |
| `LFY` | Lakeshore Flood Advisory | LS.Y |
| `LKA` | Lake Effect Snow Watch | LE.A |
| `LKW` | Lake Effect Snow Warning | LE.W |
| `LKY` | Lake Effect Snow Advisory | LE.Y |
| `LOY` | Low Water Advisory | LO.Y |
| `LWY` | Lake Wind Advisory | LW.Y |
| `MFY` | Marine Dense Fog Advisory | MF.Y |
| `MWS` | Marine Weather Statement | — |
| `RBY` | Small Craft Advisory for Rough Bar | RB.Y |
| `RFW` | Red Flag Warning | FW.W |
| `RPS` | Rip Current Statement | RP.S |
| `SCY` | Small Craft Advisory | SC.Y |
| `SEA` | Hazardous Seas Watch | SE.A |
| `SEW` | Hazardous Seas Warning | SE.W |
| `SIY` | Small Craft Advisory for Winds | SI.Y |
| `SMY` | Dense Smoke Advisory | MS.Y, SM.Y |
| `SRA` | Storm Watch | SR.A |
| `SRW` | Storm Warning | SR.W |
| `SUW` | High Surf Warning | SU.W |
| `SUY` | High Surf Advisory | SU.Y |
| `SWY` | Small Craft Advisory for Hazardous Seas | SW.Y |
| `TSY` | Tsunami Advisory | TS.Y |
| `TYA` | Typhoon Watch | TY.A |
| `TYS` | Typhoon Local Statement | — |
| `TYW` | Typhoon Warning | TY.W |
| `UPA` | Heavy Freezing Spray Watch | UP.A |
| `UPW` | Heavy Freezing Spray Warning | UP.W |
| `UPY` | Freezing Spray Advisory | UP.Y |
| `WCA` | Wind Chill Watch | WC.A |
| `WCW` | Wind Chill Warning | WC.W |
| `WCY` | Wind Chill Advisory | WC.Y |
| `WIY` | Wind Advisory | WI.Y |
| `WWY` | Winter Weather Advisory | WW.Y |
| `XHA` | Extreme Heat Watch | XH.A |
| `XHW` | Extreme Heat Warning | XH.W |
| `ZFY` | Freezing Fog Advisory | ZF.Y |
| `ZRY` | Freezing Rain Advisory | ZR.Y |

## Informational products that are intentionally *not* mapped

These api.weather.gov products are issued routinely and carry no actionable
hazard, so they have no event code and are not routed for notification. They
are dropped under any non-blank `FILTER_EVENT_CODES`. Add a mapping in
`scripts/config.py` if you want one of them promoted to a notifying event.

| Product | VTEC / code | What it is |
|---|---|---|
| Hazardous Weather Outlook | — | 7-day narrative heads-up of *potential* hazards |
| Short Term Forecast | — (`NOW`) | Near-term forecast narrative |
| Hydrologic Outlook | `HY.O` (`ESF`) | Outlook for *possible* future flooding |
| Administrative Message | `ADR` | NWS office administrative/service info |
| Test Message | — | Product/system test notices |

Any VTEC product with significance `O` (Outlook) or `N` (Synopsis) is
informational by design; only `W`/`A`/`Y`/`S` products are mapped.
