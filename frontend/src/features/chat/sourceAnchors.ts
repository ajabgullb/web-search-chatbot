/** The element id of a source card, shared by the card and the buttons citing it. */
export function sourceAnchorId(anchorPrefix: string, sourceId: string): string {
  return `${anchorPrefix}-source-${sourceId}`
}
