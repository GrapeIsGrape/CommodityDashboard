# One module per data source lives here (FRED, EIA, USDA, CFTC, yfinance).
# Each source is self-contained with its own error handling and logging so a
# single failing source cannot break the others. Implemented from Phase 2.
