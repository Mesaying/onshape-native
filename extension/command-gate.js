// One page command at a time. The local bridge already serializes commands, so a command only arrives
// while another runs when the bridge gave up waiting on the earlier one (timeout, outcome unknown).
// Then reads wait briefly for it to finish; writes are refused so nothing runs on top of an unknown outcome.
const READ_KINDS = new Set(["state", "microversion", "tabs", "schema"]);
export const isReadOnly = job => READ_KINDS.has(job?.kind) || (job?.kind === "rest" && job?.method === "GET");

export function createGate({waitMs = 20000} = {}) {
  let running = null;
  return async function run(job, execute) {
    if (running && isReadOnly(job)) {
      let timer;
      await Promise.race([running, new Promise(resolve => { timer = setTimeout(resolve, waitMs); })]);
      clearTimeout(timer);
    }
    if (running) return {error: "Previous command still running; inspect state before continuing."};
    let done;
    running = new Promise(resolve => { done = resolve; });
    try {
      return await execute(job);
    } catch (error) {
      // No request headers, cookies, or stack dumps in bridge errors.
      return {error: String(error?.message || "Page execution failed").slice(0, 500)};
    } finally {
      running = null;
      done();
    }
  };
}
