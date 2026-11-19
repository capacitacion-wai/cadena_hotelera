import { Box, keyframes } from '@mui/material'

const blink = keyframes`
  0%, 50%   { opacity: 1; }
  50.01%, 100% { opacity: 0; }
`

/**
 * Inline blinking caret rendered at the end of an assistant bubble
 * while the SSE stream is still emitting deltas. Mirrors the cursor
 * the OpenCode chat panel uses (visual continuity across surfaces).
 */
export function BlinkingCursor() {
  return (
    <Box
      component="span"
      sx={{
        display: 'inline-block',
        width: '2px',
        height: '1.05em',
        verticalAlign: '-2px',
        marginLeft: '2px',
        background: (t) => t.palette.text.primary,
        animation: `${blink} 1s steps(2, start) infinite`,
      }}
    />
  )
}

export default BlinkingCursor
