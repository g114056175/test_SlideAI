/**
 * Lightweight global event bus using the browser's built-in CustomEvent API.
 * No external dependencies required.
 *
 * Usage:
 *   import { emitter } from '@/config/events.js'
 *   emitter.emit('refresh-projects')          // sender
 *   emitter.on('refresh-projects', handler)   // receiver
 *   emitter.off('refresh-projects', handler)  // cleanup
 */

const _bus = new EventTarget()

export const emitter = {
  /**
   * Fire a named event, optionally passing a detail payload.
   * @param {string} name
   * @param {*} [detail]
   */
  emit(name, detail) {
    console.log(`[Emitter] ${name} triggered`, detail !== undefined ? detail : '')
    _bus.dispatchEvent(new CustomEvent(name, { detail }))
  },

  /**
   * Listen for a named event.
   * @param {string} name
   * @param {function} handler  receives (detail) — NOT the raw event object
   */
  on(name, handler) {
    const wrapped = (e) => handler(e.detail)
    handler._busWrapped = wrapped
    _bus.addEventListener(name, wrapped)
  },

  /**
   * Remove a previously registered listener.
   * @param {string} name
   * @param {function} handler  the same function reference passed to .on()
   */
  off(name, handler) {
    if (handler._busWrapped) {
      _bus.removeEventListener(name, handler._busWrapped)
    }
  },
}
