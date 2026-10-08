// Data layer. app.js only talks to the object returned here, so the UI is identical for the
// real Firebase backend and for the offline demo (open index.html?demo=1).
const FB = "https://www.gstatic.com/firebasejs/10.12.5/";

export function configLooksUnset(cfg) {
  return !cfg || !cfg.apiKey || String(cfg.apiKey).startsWith("YOUR_") || String(cfg.projectId).startsWith("YOUR_");
}

export async function createBackend() {
  const demo = new URLSearchParams(location.search).has("demo");
  if (demo) {
    const { DemoBackend } = await import("./demo.js");
    return new DemoBackend(new URLSearchParams(location.search));
  }
  const { firebaseConfig } = await import("./firebase-config.js");
  if (configLooksUnset(firebaseConfig)) return { mode: "unconfigured" };

  const [{ initializeApp }, A, S] = await Promise.all([
    import(FB + "firebase-app.js"), import(FB + "firebase-auth.js"), import(FB + "firebase-firestore.js"),
  ]);
  const app = initializeApp(firebaseConfig);
  const auth = A.getAuth(app);
  const db = S.getFirestore(app);
  const cb = (fn) => (err) => { console.error(err); fn && fn(err); };

  return {
    mode: "firebase",
    onAuth: (fn) => A.onAuthStateChanged(auth, fn),
    signIn: (email, pw) => A.signInWithEmailAndPassword(auth, email, pw),
    signOut: () => A.signOut(auth),
    watchStatus: (fn, onErr) => S.onSnapshot(S.doc(db, "bot", "status"),
      (s) => fn(s.exists() ? s.data() : null), cb(onErr)),
    watchCurves: (fn, onErr) => S.onSnapshot(S.doc(db, "bot", "equity"),
      (s) => fn(s.exists() ? (s.data().curves || {}) : {}), cb(onErr)),
    watchEvents: (fn, onErr) => S.onSnapshot(
      S.query(S.collection(db, "events"), S.orderBy("ts", "desc"), S.limit(150)),
      (q) => fn(q.docs.map((d) => ({ id: d.id, ...d.data() }))), cb(onErr)),
    watchTrades: (fn, onErr) => S.onSnapshot(
      S.query(S.collection(db, "trades"), S.orderBy("closed_ts", "desc"), S.limit(100)),
      (q) => fn(q.docs.map((d) => ({ id: d.id, ...d.data() }))), cb(onErr)),
    watchCommands: (fn, onErr) => S.onSnapshot(
      S.query(S.collection(db, "commands"), S.orderBy("createdAt", "desc"), S.limit(12)),
      (q) => fn(q.docs.map((d) => {
        const x = d.data({ serverTimestamps: "estimate" });
        return { id: d.id, cmd: x.cmd, args: x.args || {}, status: x.status, result: x.result || "",
                 createdAt: x.createdAt ? x.createdAt.toMillis() : Date.now() };
      })), cb(onErr)),
    async sendCommand(cmd, args) {
      const ref = await S.addDoc(S.collection(db, "commands"), {
        cmd, args: args || {}, uid: auth.currentUser.uid, status: "pending",
        createdAt: S.serverTimestamp(),
      });
      return ref.id;
    },
  };
}
