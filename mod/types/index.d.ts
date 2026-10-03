/** One open claim as the band draws it, cut from `followthrough status --json`. */
export type Claim = { id: string; title: string; state: string; dueAt: string }

/** A shipping command this session ran (deploy, merge, publish). */
export type Shipped = { command: string; at: number }

declare module 'claude-code' {
  interface PluginState {
    'followthrough-band': {
      claims: Claim[]
      shipped: Shipped[]
      isHidden: boolean
      /** The claim whose Close waits for a second press. */
      confirmClose: string | null
    }
  }
}
