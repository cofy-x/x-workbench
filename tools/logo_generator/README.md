# Logo Generator

Generate brand logo assets (`icon`, `logo`, dark variants) via:

1. Local HTTP UI (`web/index.html`)
2. CLI one-shot mode

## Run

```bash
make serve TOOL=logo_generator
```

Open `http://127.0.0.1:8000`.

## CLI Mode

```bash
make generate TOOL=logo_generator BRAND=Avant
```

## Output

Generated files are written to:

```text
generated/<brand-slug>/
```
