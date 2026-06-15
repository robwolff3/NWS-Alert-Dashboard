# NWS alert event reference

The full catalog of alert events the dashboard understands, generated from the
tables in [`scripts/config.py`](scripts/config.py) (`EVENT_GROUPS` /
`EVENT_CODE_NAMES`). The same data drives the **custom notification** picker in
the web dashboard, so this page and that picker never disagree. Use it to decide
what to put in `NOTIFY_EVENT_CODES`, the `NOTIFY_PRIORITY_*_CODES` lists, or a
per-device custom push subscription.

## How events are coded

Every alert is tagged with a 3-letter **event code** (EEE):

- **Standard SAME/EAS codes** are the official codes the National Weather
  Service broadcasts over NOAA Weather Radio
  ([weather.gov/nwr/eventcodes](https://www.weather.gov/nwr/eventcodes)). They
  can arrive from any source — radio (SAME decode), the API, or NWWS-OI.
- **Internal pseudo-codes** cover common products that have **no** official
  SAME code, so they only arrive from the API or NWWS-OI. Each gets a stable
  `phen+sig` pseudo-code (e.g. `FGY` for a Dense Fog Advisory) so it routes
  through the same filter and priority machinery as a real EAS event.

The **Radio** column marks codes the on-device SAME decoder can emit (the
official EAS set plus the decoder's extended/legacy codes). The **VTEC** column
shows the `phenomenon.significance` pair(s) that map to each code; blank means
the product is matched by event name only. Two pseudo-codes are deliberately
*not* `phen+sig` to avoid colliding with a real code: Lake Effect Snow uses
`LK*` (`LEW` is Law Enforcement Warning) and Lakeshore Flood uses `LF*` (`LSW`
is Land Slide Warning in the decoder's extended set).

## What to do with these

`NOTIFY_EVENT_CODES` is an **opt-in allowlist**:

- **Blank (the default) = accept everything.** Every event below notifies.
- **Non-blank = only the listed codes notify.** Any event *not* in your list is
  still ingested and shown on the dashboard, but stays silent (no push, no
  Apprise, no MQTT). An unmapped/informational product (one with no EEE at all)
  is likewise treated as filtered whenever a non-blank list is set.

You usually do **not** need to prune by geography. api.weather.gov only returns
alerts for the **zones and county you configured** (`FILTER_ZONES` /
`FILTER_SAME_CODES`, both auto-derived from `LOCATION`). An inland location
never receives a Small Craft Advisory or Storm Surge Warning, so leaving the
marine and tropical codes in your allowlist costs nothing. Trim
`NOTIFY_EVENT_CODES` only to silence event *types* you don't care about (for
example, dropping the advisory-tier codes if you only want warnings).

Two independent layers sit on top:

- **Priority routing** — `NOTIFY_PRIORITY_{5,4,3}_CODES` map a code to a
  notification priority (which ntfy turns into an Android channel). A code in
  none of those lists falls back to `NTFY_PRIORITY_DEFAULT`.
- **Per-device web push** — each browser subscription can either follow a
  minimum priority or pick an explicit set of codes from the dashboard's custom
  notification panel (the groups below).

See [`.env.example`](.env.example) for the shipped defaults.

## Event codes by category

147 selectable codes across 12 groups
(58 standard SAME/EAS, 89 internal pseudo-codes).

### Tornado

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `TOR` | Tornado Warning | TO.W | ✓ |
| `TOA` | Tornado Watch | TO.A | ✓ |

### Thunderstorm & Wind

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `SVR` | Severe Thunderstorm Warning | SV.W | ✓ |
| `SVA` | Severe Thunderstorm Watch | SV.A | ✓ |
| `SVS` | Severe Weather Statement | — | ✓ |
| `EWW` | Extreme Wind Warning | EW.W | ✓ |
| `SQW` | Snow Squall Warning | SQ.W | ✓ |
| `SPS` | Special Weather Statement | — | ✓ |
| `HWW` | High Wind Warning | HW.W | ✓ |
| `HWA` | High Wind Watch | HW.A | ✓ |
| `WIY` | Wind Advisory | WI.Y | — |
| `LWY` | Lake Wind Advisory | LW.Y | — |
| `BWY` | Brisk Wind Advisory | BW.Y | — |

### Winter, Ice & Cold

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `WSW` | Winter Storm Warning | WS.W | ✓ |
| `WSA` | Winter Storm Watch | WS.A | ✓ |
| `BZW` | Blizzard Warning | BZ.W | ✓ |
| `BZA` | Blizzard Watch | BZ.A | — |
| `WWY` | Winter Weather Advisory | WW.Y | — |
| `ISW` | Ice Storm Warning | IS.W | — |
| `LKW` | Lake Effect Snow Warning | LE.W | — |
| `LKA` | Lake Effect Snow Watch | LE.A | — |
| `LKY` | Lake Effect Snow Advisory | LE.Y | — |
| `ZRY` | Freezing Rain Advisory | ZR.Y | — |
| `FSW` | Flash Freeze Warning | — | ✓ |
| `BSY` | Blowing Snow Advisory | BS.Y | — |
| `FZW` | Freeze Warning | FZ.W | ✓ |
| `FZA` | Freeze Watch | FZ.A | — |
| `HZW` | Hard Freeze Warning | HZ.W | — |
| `HZA` | Hard Freeze Watch | HZ.A | — |
| `FRY` | Frost Advisory | FR.Y | — |
| `CWY` | Cold Weather Advisory | CW.Y | — |
| `WCW` | Wind Chill Warning | WC.W | — |
| `WCY` | Wind Chill Advisory | WC.Y | — |
| `WCA` | Wind Chill Watch | WC.A | — |
| `ECW` | Extreme Cold Warning | EC.W | — |
| `ECA` | Extreme Cold Watch | EC.A | — |
| `AVW` | Avalanche Warning | — | ✓ |
| `AVA` | Avalanche Watch | — | ✓ |

### Heat

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `EHW` | Excessive Heat Warning | EH.W | — |
| `EHA` | Excessive Heat Watch | EH.A | — |
| `HTY` | Heat Advisory | HT.Y | — |
| `XHW` | Extreme Heat Warning | XH.W | — |
| `XHA` | Extreme Heat Watch | XH.A | — |

### Fog, Dust & Smoke

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `FGY` | Dense Fog Advisory | FG.Y | — |
| `ZFY` | Freezing Fog Advisory | ZF.Y | — |
| `DSW` | Dust Storm Warning | DS.W | ✓ |
| `DUW` | Blowing Dust Warning | DU.W | — |
| `DUY` | Blowing Dust Advisory | DU.Y | — |
| `SMY` | Dense Smoke Advisory | MS.Y, SM.Y | — |
| `ASY` | Air Stagnation Advisory | AS.Y | — |
| `AQA` | Air Quality Alert | — | — |

### Flood

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `FFW` | Flash Flood Warning | FF.W | ✓ |
| `FFA` | Flash Flood Watch | FF.A | ✓ |
| `FFS` | Flash Flood Statement | — | ✓ |
| `FLW` | Flood Warning | FA.W, FL.W | ✓ |
| `FLA` | Flood Watch | FA.A, FL.A | ✓ |
| `FLS` | Flood Statement | FA.Y, FL.Y | ✓ |
| `CFW` | Coastal Flood Warning | CF.W | ✓ |
| `CFA` | Coastal Flood Watch | CF.A | ✓ |
| `CFY` | Coastal Flood Advisory | CF.Y | — |
| `CFS` | Coastal Flood Statement | CF.S | — |
| `LFW` | Lakeshore Flood Warning | LS.W | — |
| `LFA` | Lakeshore Flood Watch | LS.A | — |
| `LFY` | Lakeshore Flood Advisory | LS.Y | — |
| `LFS` | Lakeshore Flood Statement | LS.S | — |
| `HYY` | Hydrologic Advisory | HY.Y | — |
| `DBA` | Dam Watch | — | ✓ |
| `DBW` | Dam Break Warning | — | ✓ |

### Marine & Tropical

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `HUW` | Hurricane Warning | HU.W | ✓ |
| `HUA` | Hurricane Watch | HU.A | ✓ |
| `HLS` | Hurricane Local Statement | — | ✓ |
| `TRW` | Tropical Storm Warning | TR.W | ✓ |
| `TRA` | Tropical Storm Watch | TR.A | ✓ |
| `SSW` | Storm Surge Warning | SS.W | ✓ |
| `SSA` | Storm Surge Watch | SS.A | ✓ |
| `TYW` | Typhoon Warning | TY.W | — |
| `TYA` | Typhoon Watch | TY.A | — |
| `TYS` | Typhoon Local Statement | — | — |
| `TSW` | Tsunami Warning | TS.W | ✓ |
| `TSA` | Tsunami Watch | TS.A | ✓ |
| `TSY` | Tsunami Advisory | TS.Y | — |
| `SMW` | Special Marine Warning | MA.W | ✓ |
| `GLW` | Gale Warning | GL.W | — |
| `GLA` | Gale Watch | GL.A | — |
| `SRW` | Storm Warning | SR.W | — |
| `SRA` | Storm Watch | SR.A | — |
| `HFW` | Hurricane Force Wind Warning | HF.W | — |
| `HFA` | Hurricane Force Wind Watch | HF.A | — |
| `SEW` | Hazardous Seas Warning | SE.W | — |
| `SEA` | Hazardous Seas Watch | SE.A | — |
| `SCY` | Small Craft Advisory | SC.Y | — |
| `SIY` | Small Craft Advisory for Winds | SI.Y | — |
| `RBY` | Small Craft Advisory for Rough Bar | RB.Y | — |
| `SWY` | Small Craft Advisory for Hazardous Seas | SW.Y | — |
| `MFY` | Marine Dense Fog Advisory | MF.Y | — |
| `MWS` | Marine Weather Statement | — | — |
| `LOY` | Low Water Advisory | LO.Y | — |
| `UPW` | Heavy Freezing Spray Warning | UP.W | — |
| `UPA` | Heavy Freezing Spray Watch | UP.A | — |
| `UPY` | Freezing Spray Advisory | UP.Y | — |
| `SUW` | High Surf Warning | SU.W | — |
| `SUY` | High Surf Advisory | SU.Y | — |
| `RPS` | Rip Current Statement | RP.S | — |
| `BHS` | Beach Hazards Statement | BH.S | — |

### Fire

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `RFW` | Red Flag Warning | FW.W | — |
| `FWA` | Fire Weather Watch | FW.A | — |
| `WFW` | Wildfire Warning | — | ✓ |
| `WFA` | Wildfire Watch | — | ✓ |
| `FRW` | Fire Warning | — | ✓ |
| `IFW` | Industrial Fire Warning | — | ✓ |

### Geophysical

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `EQW` | Earthquake Warning | — | ✓ |
| `VOW` | Volcano Warning | — | ✓ |
| `AFW` | Ashfall Warning | AF.W | — |
| `AFY` | Ashfall Advisory | AF.Y | — |
| `LSW` | Landslide Warning | — | ✓ |

### Civil Emergency

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `EAN` | Emergency Action Notification | — | ✓ |
| `EAT` | Emergency Action Termination | — | ✓ |
| `NIC` | National Information Center | — | ✓ |
| `NMN` | Network Message Notification | — | ✓ |
| `LAE` | Local Area Emergency | — | ✓ |
| `CEM` | Civil Emergency Message | — | ✓ |
| `CDW` | Civil Danger Warning | — | ✓ |
| `CAE` | Child Abduction Emergency | — | ✓ |
| `EVI` | Evacuation – Immediate | — | ✓ |
| `EVA` | Evacuation Watch | — | ✓ |
| `LEW` | Law Enforcement Warning | — | ✓ |
| `SPW` | Shelter In Place Warning | — | ✓ |
| `BLU` | Blue Alert | — | — |
| `TOE` | 911 Telephone Outage Emergency | — | ✓ |

### Hazards & Utility

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `HMW` | Hazardous Materials Warning | — | ✓ |
| `NUW` | Nuclear Power Plant Warning | — | ✓ |
| `RHW` | Radiological Hazard Warning | — | ✓ |
| `CHW` | Chemical Hazard Warning | — | ✓ |
| `CWW` | Contaminated Water Warning | — | ✓ |
| `BHW` | Biological Hazard Warning | — | ✓ |
| `BWW` | Boil Water Warning | — | ✓ |
| `DEW` | Contagious Disease Warning | — | ✓ |
| `FCW` | Food Contamination Warning | — | ✓ |
| `POS` | Power Outage Statement | — | ✓ |
| `IBW` | Iceberg Warning | — | ✓ |

### Tests & Administrative

| Code | Event | VTEC | Radio |
|---|---|---|:--:|
| `RWT` | Required Weekly Test | — | ✓ |
| `RMT` | Required Monthly Test | — | ✓ |
| `NPT` | National Periodic Test | — | ✓ |
| `NST` | National Silent Test | — | ✓ |
| `NAT` | National Audible Test | — | ✓ |
| `DMO` | Practice/Demo Warning | — | ✓ |
| `ADR` | Administrative Message | — | ✓ |

## Informational products that are intentionally *not* mapped

These api.weather.gov products are issued routinely and carry no actionable
hazard, so they have no event code and are not routed for notification. They
are dropped under any non-blank `NOTIFY_EVENT_CODES`. Add a mapping in
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
