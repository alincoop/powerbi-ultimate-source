# Power BI Report Source Systems

Lists every Power BI Report in Collibra and the System(s) its data ultimately comes from.

Uses only the publicly documented [Collibra Knowledge Graph (GraphQL) API](https://developer.collibra.com/api/guides/knowledge-graph), via `POST /graphql/knowledgeGraph/v1`.

## How it works

1. Finds all assets of type **Power BI Report**.
2. Walks upstream through catalog relations:

   ```
   Power BI Report      <- is source for -  Power BI Data Model
   Power BI Data Model  <- is part of -     Power BI Table
   Power BI Table       - contains ->       Power BI Column
   Power BI Column      <- targets -        Column  <- targets -  Column ...  (repeats until nothing is further upstream)
   ```

3. The furthest-left assets are the ultimate sources. Leaves that are still Power BI assets, such as calculated columns, are ignored.
4. Climbs from each source to its System:

   ```
   Column - is part of -> Table <- contains - Schema <- has - Database <- groups - System
   ```

The walk goes one level at a time and fetches many assets per query, so it needs relatively few API calls.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests
```

## Run

```bash
COLLIBRA_URL=https://your-instance.collibra.com \
COLLIBRA_USER=your-username \
COLLIBRA_PASSWORD=your-password \
python powerbi_ultimate_sources.py
```

In a new terminal, run `source .venv/bin/activate` again first.

## Output

```
Report Name              Report ID     System Name  System ID
-----------------------  ------------  -----------  ------------
compliance hero journey  01a07c4c-...  Databricks   019bbb8c-...
```

- One row per report and system. A report fed by two systems gets two rows.
- If no System is linked, the highest container found is shown with its type, e.g. `demo [Database; no System linked]`.
- `(no upstream source found)` means the report's catalog relations don't lead to anything outside Power BI.
- Progress messages go to stderr; the table goes to stdout.

## Notes

- Results reflect **catalog relations**, so they're only as complete as those relations. If column-level "targets" relations are missing between layers, the walk stops early at the last linked column.
- A Data Model feeds every report built on it, so all reports on the same Data Model get the same systems.
- The relation types followed are the out-of-the-box ones, listed as constants at the top of the script. If your instance links assets differently (for example other BI tools, files, or custom relation types), add those relation type IDs to `UPSTREAM_INCOMING`, `UPSTREAM_OUTGOING`, `PARENT_OUTGOING` or `PARENT_INCOMING`.
- Assets your user can't see are shown as `(not accessible)` and treated as dead ends.
- If Collibra rate-limits the script (HTTP 429), it waits and retries automatically. If it still fails, raise `REQUEST_DELAY` at the top of the script.
