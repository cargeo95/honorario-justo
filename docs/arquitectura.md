# Arquitectura

Honorario Justo está organizado en capas. Cada una depende solo de las de abajo, y el
dominio no depende de nada externo: se puede probar sin red, sin PDF y sin base de datos.

```mermaid
flowchart TD
    CLI["cli<br/><small>honorario-justo</small>"]
    SRV["servicios<br/><small>casos de uso</small>"]
    REP["reportes<br/><small>salidas</small>"]
    FUE["fuentes<br/><small>SECOP II · PDF · OCR</small>"]
    ALM["almacenamiento<br/><small>SQLite</small>"]
    DOM["dominio<br/><small>reglas del negocio</small>"]

    CLI --> SRV
    CLI --> REP
    SRV --> FUE
    SRV --> ALM
    SRV --> DOM
    REP --> ALM
    REP --> DOM
    FUE --> DOM
    ALM --> DOM

    classDef dominio fill:#a738ea,color:#fff,stroke:#a738ea
    classDef borde fill:#fed353,color:#111827,stroke:#fed353
    class DOM dominio
    class FUE,ALM borde
```

## Capas

| Capa | Módulos | Responsabilidad |
|---|---|---|
| `dominio` | `texto`, `extraccion`, `aprendizaje`, `formato` | Qué es un cargo, un pago mensual, la experiencia exigida y el SMLV de cada año. Señales de la página y reporte de aprendizaje. |
| `fuentes` | `secop`, `documentos`, `ocr.cjs` | API de Datos Abiertos (SECOP II), descarga de PDF, extracción de texto, OCR y puntuación de páginas. |
| `almacenamiento` | `base_datos` | Esquema SQLite, casos, hallazgos, resumen por día y copias de seguridad. |
| `servicios` | `analisis`, `busqueda`, `hallazgos`, `corte_diario`, `aprendizaje` | Casos de uso: analizar un proceso, corte diario, extraer y revisar hallazgos, aprender de las revisiones. |
| `reportes` | `indicadores`, `tablero`, `imagenes`, `investigacion` | Resumen por día, tablero HTML, imágenes para LinkedIn y TikTok, expediente de cada caso. |
| `cli` | `cli`, `__main__` | Comando `honorario-justo`. |

## Flujo del corte diario

```mermaid
sequenceDiagram
    participant U as Editor
    participant C as corte_diario
    participant S as SECOP II
    participant D as documentos
    participant H as hallazgos
    participant B as SQLite

    U->>C: honorario-justo --dia
    C->>S: procesos de mínima cuantía del día
    loop cada proceso
        C->>S: índice de documentos
        C->>D: descarga PDF, texto y OCR
        D-->>C: páginas puntuadas (objeto, experiencia, presupuesto)
        C->>H: extraer cargo, años y pago
        H->>B: hallazgos sin_revisar
    end
    C->>B: resumen del día y respaldo
    U->>H: --marcar ID confirmado | descartado
    H->>B: etiqueta de aprendizaje
```

## Decisiones

- **Trazabilidad antes que cobertura.** Cada hallazgo guarda archivo y página de origen. Una cifra sin fuente no se publica.
- **Revisión humana obligatoria.** La extracción automática propone; el editor confirma. Solo lo `confirmado` llega a los reportes publicables.
- **Aprender con evidencia.** Las reglas para saltar documentos que no aportan (por ejemplo, no hacer OCR a suministros) solo se proponen cuando `acumulado.md` junta al menos 30 revisiones. Nunca se aplican solas.
- **Local y reanudable.** SQLite, caché de texto por hash y OCR local. Un corte interrumpido retoma donde iba.
- **Sin efectos al importar.** El logging se configura en la CLI y la base se crea al primer uso, así cada módulo se puede importar y probar por separado.
