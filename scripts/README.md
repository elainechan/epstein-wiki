# Scripts
## Running `download.go` Dowload Data

- Build
```
go build -o download scripts/download.go
```

- Run 
```
./download -out /absolute/path/to/wherever
```
./download -out /Volumes/Bones/epstein-files

- On termination due to error:
```
./download -reset            # ignore all checkpoints, re-scrape everything
./download -reset -datasets 4  # reset just dataset 4
```

- Download individual sections:
```
./download -out /path                        # all four sections
./download -out /path -sections efta         # just datasets 1-12
./download -out /path -sections court,foia   # court records + FOIA
./download -out /path -sections prior        # prior DOJ disclosures only

# log written to ./download.log automatically
tail -f download.log   # follow in another terminal
```

./download -out /Volumes/Bones/epstein-files -sections efta
./download -out /Volumes/Bones/epstein-files -sections court
./download -out /Volumes/Bones/epstein-files -sections foia
./download -out /Volumes/Bones/epstein-files -sections prior



```
./download -out /Volumes/Bones/epstein-files -sections prior
./download -out /Volumes/Bones/epstein-files -sections foia
```


./download -out /Volumes/Bones/epstein-files -sections court
══════════════════════════════════════════════════════════════
Section           Found Downloaded  Skipped   Errors
──────────────────────────────────────────────────────
court               579        205        0      374
──────────────────────────────────────────────────────
TOTAL               579        205        0      374


./download -out /Volumes/Bones/epstein-files -sections court

```
./download -out /Volumes/Bones/epstein-files -sections efta -skip-index \
  -workers 4 -file-workers 4 -delay 800 -page-delay 2000 \
  -cookie "ak_bmsc=549ACA18743DD436BCAACF25D9A9D9C7~000000000000000000000000000000~YAAQzFQhFyacSemeAQAARpuI8gDZqW0CVV7ySQVtDS0VQTzOpv9ozTXCy00x4DbaXZYxjouEt/3AGRmjZmS6+7dHKnMj3zuU9gu0Hr4USoqWuZWfBHeWmCKUXMMX1IBEuVxXv4A89loXoNMr50iB9hO3V7AZHfchuFmxPYGgnhM05ZlBqC+dpNDsw5EDmqpk2SQHEAGa+g8HZGfyHs9fp6S8YddCdSeEJDMJOag2W1xo8UL74gpuWzYWtdVP1gKrGqyr82dNxiWtiFgM6pL7DO9XGmQTGiGWhV0/oGCNiZkmQgTkDeiDwbsFpRIGrq7u9aI8GANHJd+V2JCXs7yC5NyWk1Kp05pFBiXBEsNu21lXm7HsOsk5QA9J/9kF0Pwp+ZkDyQ=="
```

## Running `ingest.py` to Ingest Downloaded Data

Point it at a directory:
```
  .venv/bin/python scripts/ingest.py \
    --dir /Volumes/Bones/epstein-files/prior-doj \
    --skip-semiont \
    --skip-vision
```

## Ingestion Validation 06252026

All 6 queries validated. Results:
  
```
  +---+-------------------------------------+------------------+--------+--------------------------------------------------+
  | # | Query                               | Tool             | Route  | Top Result                                       |
  +---+-------------------------------------+------------------+--------+--------------------------------------------------+
  | 1 | Who recruited underage victims      | query-kb         | HYBRID | EFTA00014638 — Epstein paid victim-recruiters    |
  |   |                                     |                  |        | cash to bring additional minors                  |
  +---+-------------------------------------+------------------+--------+--------------------------------------------------+
  | 2 | Banks that handled Epstein accounts | query-kb         | HYBRID | EFTA01289201 — Deutsche Bank/Pershing docs       |
  |   |                                     |                  |        | (use filter-by-entity for precision)             |
  +---+-------------------------------------+------------------+--------+--------------------------------------------------+
  | 3 | Documents mentioning Deutsche Bank  | filter-by-entity | BM25   | 60 docs — account statements, wire transfers,    |
  |   |                                     |                  |        | USVI v JPMorgan filings                          |
  +---+-------------------------------------+------------------+--------+--------------------------------------------------+
  | 4 | Plea deal / NPA 2008 Florida        | query-kb         | EXACT  | EFTA02740750 — NPA effective June 30 2008,       |
  |   |                                     |                  |        | USAO Southern District FL, 3 case files          |
  +---+-------------------------------------+------------------+--------+--------------------------------------------------+
  | 5 | Flight logs / passenger names       | query-kb         | HYBRID | EFTA00008920 — "JE, GM" + unnamed "female",      |
  |   |                                     |                  |        | minors as young as 16                            |
  +---+-------------------------------------+------------------+--------+--------------------------------------------------+
  | 6 | JPMorgan knew sex trafficking       | query-kb         | HYBRID | EFTA02809392 — USVI v JPMorgan: "JPMorgan Knew  |
  |   |                                     |                  |        | Epstein Was Engaged in Human Trafficking"        |
  +---+-------------------------------------+------------------+--------+--------------------------------------------------+
```