function hasExited(child) {
  return !child?.pid || child.exitCode !== null || child.signalCode != null;
}

function waitForExit(child, timeout) {
  if (hasExited(child)) return Promise.resolve(true);
  return new Promise(resolve => {
    let timer;
    function finish(exited) {
      clearTimeout(timer);
      child.removeListener('exit', onExit);
      resolve(exited);
    }
    function onExit() { finish(true); }
    child.once('exit', onExit);
    timer = setTimeout(() => finish(false), timeout);
    if (hasExited(child)) finish(true);
  });
}

module.exports = { hasExited, waitForExit };
