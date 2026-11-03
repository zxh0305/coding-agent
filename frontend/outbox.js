// Each write owns one message. Tabs sending different messages cannot erase each other's queue.
const Outbox = (() => {
  let database;
  function open() {
    if (!database) database = new Promise((resolve, reject) => {
      const req = indexedDB.open("coding-agent-outbox", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("messages", {keyPath: ["owner", "nonce"]});
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => { database = null; reject(req.error); };
    });
    return database;
  }
  async function access(mode, operation) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("messages", mode);
      const request = operation(tx.objectStore("messages"));
      tx.oncomplete = () => resolve(request.result);
      tx.onabort = () => reject(tx.error || new Error("本地消息保存失败"));
    });
  }
  return {
    put: item => access("readwrite", store => store.put(item)),
    remove: item => access("readwrite", store => store.delete([item.owner, item.nonce])),
    list: owner => access("readonly", store => store.getAll(IDBKeyRange.bound([owner], [owner, []]))),
  };
})();
