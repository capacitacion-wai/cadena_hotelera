/**
 * THIS EMPLOYEE'S THEME — its colors, typography and measures. It is ITS OWN (not an
 * invariant): the platform writes it from the decisions of its Definition (`derivar_tema` of
 * the wai-agent-platform MCP) and `theme.ts` turns it into the MUI theme with `construirTema`.
 *
 * Not edited by hand: change the decisions and regenerate. What goes between the «tema» and
 * «fin» comment marks is pure JSON — the platform and the verifier read it from there.
 *
 * The one the scaffold brings is the usual WAI look, exactly: an employee older than the
 * design system gets upgraded and looks the same.
 */
import type { TemaWai } from './construirTema'

export const TEMA: TemaWai = /*tema*/{
  "formato": 1,
  "nombre": "WAI — empleado",
  "decisiones": {
    "origen": "wai",
    "nota": "El aspecto de los empleados antes del sistema de diseño, exacto: un empleado que se actualiza se ve igual."
  },
  "modos": {
    "claro": {
      "fondo": "#F4F7FC",
      "papel": "#FFFFFF",
      "papel2": "#F7F9FD",
      "texto": "#14203A",
      "texto_suave": "#5C6B86",
      "texto_tenue": "#8695AE",
      "borde": "rgba(133, 149, 174, 0.18)",
      "sombra": "#8595AE",
      "primario": "#2C5BE0",
      "primario_oscuro": "#1E45B8",
      "primario_claro": "#6E93FF",
      "sobre_primario": "#FFFFFF",
      "secundario": "#12A06A",
      "acento": "#E8890B",
      "aviso": "#C6830A",
      "peligro": "#DD4B4B",
      "exito": "",
      "info": "",
      "cabecera_tabla": "#F1F4FA",
      "fila_hover": "rgba(44, 91, 224, 0.045)",
      "lateral": "#0B1220",
      "lateral_texto": "#C4CDD5",
      "gris50": "",
      "gris100": ""
    },
    "oscuro": {
      "fondo": "#0A0F1C",
      "papel": "#111A2C",
      "papel2": "#0D1524",
      "texto": "#EAF0FB",
      "texto_suave": "#8FA0BC",
      "texto_tenue": "#5D6E8C",
      "borde": "rgba(93, 110, 140, 0.28)",
      "sombra": "#000000",
      "primario": "#6E93FF",
      "primario_oscuro": "#4C6BE8",
      "primario_claro": "#9DB8FF",
      "sobre_primario": "rgba(0, 0, 0, 0.87)",
      "secundario": "#4FE3B0",
      "acento": "#FF9E64",
      "aviso": "#F5B84A",
      "peligro": "#FF6E6E",
      "exito": "",
      "info": "",
      "cabecera_tabla": "#0D1524",
      "fila_hover": "rgba(110, 147, 255, 0.08)",
      "lateral": "#0B1220",
      "lateral_texto": "#C4CDD5",
      "gris50": "#0F1826",
      "gris100": "#18233A"
    }
  },
  "tipografia": {
    "cuerpo": "Inter",
    "titulos": "Inter",
    "pesos": {
      "h3": 820,
      "h4": 800,
      "h5": 750,
      "h6": 720,
      "subtitle1": 600,
      "caption": 500,
      "boton": 640
    },
    "espaciado": {
      "h3": "-0.025em",
      "h4": "-0.02em",
      "h5": "-0.015em",
      "boton": "0.01em"
    }
  },
  "forma": {
    "radio": 12,
    "tarjeta": 16,
    "boton": 11,
    "dialogo": 18,
    "aviso": 9,
    "lista": 10,
    "barra": 5
  },
  "densidad": {
    "boton": "7px 17px",
    "cabecera_celda": "12px 16px",
    "celda": "10px 16px"
  },
  "componentes": {
    "tarjeta_con_borde": true,
    "lateral_con_degradado": true,
    "aurora": {
      "claro": [
        "#4C6BE8",
        "#8A5BE0",
        "#22B79A"
      ],
      "oscuro": [
        "#6E8BFF",
        "#9B6BFF",
        "#4FE3C0"
      ]
    }
  },
  "graficos": [
    "#332288",
    "#117733",
    "#88CCEE",
    "#DDCC77",
    "#CC6677",
    "#AA4499",
    "#882255"
  ],
  "estados": {
    "ok": "#117733",
    "warn": "#DDCC77",
    "error": "#CC6677",
    "neutral": "#546e7a",
    "info": "#332288",
    "reclamado": "#88CCEE",
    "aceptado": "#2e7d32",
    "rechazado": "#c62828",
    "sin_respuesta": "#9e9e9e"
  },
  "rangos": [
    "#c62828",
    "#ef6c00",
    "#1565c0",
    "#2e7d32"
  ]
}/*fin*/
