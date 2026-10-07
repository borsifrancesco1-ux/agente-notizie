/**
 * Worker di Cloudflare per il bot Telegram dell'agente notizie.
 *
 * Telegram consegna qui ogni aggiornamento del bot (webhook), così il bot risponde subito:
 * - voti 👍/👎 sotto le notizie: li registra in forma anonima e aggiorna il conteggio sui pulsanti;
 * - messaggi del proprietario: li mette in coda come comandi e fa partire subito l'agente su GitHub;
 * - /iscrivimi, /disiscrivimi, /iscrizioni: li gestisce qui, con risposta immediata, per chiunque;
 * - /iscrivi, /disiscrivi, /link (solo il proprietario): iscrive d'ufficio un membro del team, che riceve
 *   le notizie appena avvia il bot (Telegram non lascia scrivere a chi non l'ha mai avviato);
 * - /argomenti scritto dal proprietario nel gruppo del team: crea un argomento per reparto, così l'agente
 *   pubblica ogni notizia nell'argomento dei suoi reparti invece che nel canale;
 * - /reparto <reparti> scritto dal proprietario in un gruppo: quel gruppo riceve le notizie di quei reparti
 *   (un gruppo per reparto, in più del canale);
 * - pulsanti della proposta di profilo: diventano il comando /proposta applica|ignora <id>.
 * L'agente (agente.py) a ogni giro ritira da qui voti, comandi, iscrizioni, argomenti e gruppi dei reparti
 * (/agente/voti, /agente/coda, /agente/iscrizioni, /agente/gruppo, /agente/reparti).
 * Fa anche da orologio: ogni ora avvia l'agente su GitHub (vedi "scheduled").
 *
 * Segreti (npx wrangler secret put): TELEGRAM_TOKEN, WEBHOOK_SECRET, AGENTE_KEY, OWNER_ID, GITHUB_TOKEN.
 * Variabili in wrangler.toml: REPO, ACCESSO. Archivio: KV con nome "STATO".
 */

const AIUTO = `Comandi del bot (oppure scrivimi in italiano normale, es. "segui anche Mediobanca"):

Cosa seguire
/segui <società> – aggiungo un titolo da seguire
/tema <argomento> – aggiungo un tema da seguire
/smetti <nome> – smetto di seguire un titolo o un tema
/soglia <1-10> – cambio la soglia di rilevanza (più bassa = più notizie)
/profilo <frase> – aggiungo un'indicazione al profilo
/annulla – annullo l'ultima modifica fatta con questi comandi

Notizie e domande
/chiedi <domanda> – rispondo usando l'archivio delle notizie, con le fonti
/azienda <società> [domanda] – dati e risposte su una società quotata negli USA
   (bilanci, multipli, crescita, dividendi, rischio, conference call, DCF in Excel, report),
   es. /azienda Apple com'è andato l'ultimo trimestre?
/oggi – le notizie inviate oggi, per reparto
/cerca <parole> – cerco nell'archivio delle notizie
/notizie – faccio subito un giro di notizie
/stato – com'è andata oggi

Invii
/pausa <durata> – sospendo gli invii (es. /pausa 3h, /pausa 2g)
/riprendi – riprendo gli invii
/iscrivimi <reparti> [subito|sera] – notizie dei tuoi reparti in privato
/disiscrivimi – smetto di mandarti le notizie in privato
/iscrizioni – a cosa sei iscritto (per chi gestisce il bot: tutti gli iscritti)

Iscrivere il team (solo chi gestisce il bot)
/iscrivi @utente <reparti> [sera] – iscrivo un membro del team; riceve le notizie appena avvia il bot
/disiscrivi @utente – tolgo l'iscrizione di un membro
/link <reparti> – un link da mandare al team: chi lo apre e preme Avvia è già iscritto
/argomenti – scritto nel gruppo del team: creo un argomento per reparto e pubblico lì le notizie
/reparto <reparti> – scritto in un gruppo: gli mando le notizie di quei reparti (/reparto nessuno per smettere)
/aiuto – questo elenco`;

const AIUTO_MEMBRI = `Ciao! Questo bot pubblica le notizie del canale del team WhiteRock.
Puoi riceverle anche in privato, solo quelle dei tuoi reparti:
/iscrivimi <reparti> [sera] – es. /iscrivimi Obbligazionario Copertura
   (reparti: ${["Macroeconomia", "Geopolitica", "Azionario", "Obbligazionario", "Copertura", "Risk management"].join(", ")}, oppure "tutti";
   con "sera" ricevi un solo riepilogo alle 22)
/iscrizioni – a cosa sei iscritto
/disiscrivimi – smetto di mandartele`;

const REPARTI = ["Macroeconomia", "Geopolitica", "Azionario", "Obbligazionario", "Copertura", "Risk management"];
// colori ammessi da Telegram per l'icona degli argomenti, uno per reparto
const COLORI_ARGOMENTI = [0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F];
// parole accettate dopo /iscrivimi, oltre ai nomi dei reparti
const SINONIMI = {
  macro: "Macroeconomia", geo: "Geopolitica", azioni: "Azionario", obbligazioni: "Obbligazionario",
  bond: "Obbligazionario", cambi: "Copertura", fx: "Copertura", risk: "Risk management", rischi: "Risk management",
};

export default {
  // Orologio: ogni ora fa partire l'agente su GitHub (orari in wrangler.toml, in UTC).
  // È più puntuale degli orari di GitHub, che restano come riserva.
  async scheduled(evento, env, ctx) {
    ctx.waitUntil(avviaAgente(env, "orario"));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/telegram" && request.method === "POST") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
        return new Response("non autorizzato", { status: 403 });
      }
      try {
        await gestisciAggiornamento(await request.json(), env);
      } catch (errore) {
        // Rispondo comunque "ok": altrimenti Telegram ripeterebbe lo stesso aggiornamento
        console.log("errore:", errore && errore.message);
      }
      return new Response("ok");
    }

    if (url.pathname.startsWith("/agente/")) {
      if (request.headers.get("Authorization") !== `Bearer ${env.AGENTE_KEY}`) {
        return new Response("non autorizzato", { status: 403 });
      }
      if (url.pathname === "/agente/voti") return json(await leggi(env, "voti", {}));
      if (url.pathname === "/agente/iscrizioni") return json(await leggi(env, "iscrizioni", {}));
      if (url.pathname === "/agente/gruppo") return json(await leggi(env, "gruppo", {}));
      if (url.pathname === "/agente/reparti") return json(await leggi(env, "gruppi_reparti", {}));
      if (url.pathname === "/agente/coda" && request.method === "GET") return json(await leggi(env, "coda", []));
      if (url.pathname === "/agente/coda" && request.method === "DELETE") {
        const { ids = [] } = await request.json();
        const coda = (await leggi(env, "coda", [])).filter((comando) => !ids.includes(comando.id));
        await env.STATO.put("coda", JSON.stringify(coda));
        return json({ rimasti: coda.length });
      }
    }

    return new Response("Bot dell'agente notizie WhiteRock: attivo.");
  },
};

async function gestisciAggiornamento(aggiornamento, env) {
  if (aggiornamento.callback_query) return gestisciPulsante(aggiornamento.callback_query, env);

  const messaggio = aggiornamento.message;
  if (!messaggio || !messaggio.text) return;
  const chat = messaggio.chat.id;
  const proprietario = String(messaggio.from.id) === String(env.OWNER_ID);
  const testo = messaggio.text.trim();
  const comando = testo.split(/[\s@]/)[0].toLowerCase();

  // Nei gruppi il bot ascolta solo /argomenti e /reparto; tutto il resto si fa in privato
  if (messaggio.chat.type === "group" || messaggio.chat.type === "supergroup") {
    if (comando === "/argomenti") return creaArgomenti(messaggio, proprietario, env);
    if (comando === "/reparto") return assegnaReparto(messaggio, proprietario, testo, env);
    return;
  }
  if (messaggio.chat.type !== "private") return;

  // Chi era stato iscritto d'ufficio (/iscrivi) lo diventa davvero al primo messaggio: ora il bot può scrivergli
  const attivata = await attivaPreiscrizione(messaggio, env);
  if (comando === "/start") {
    const reparti = testo.split(/\s+/)[1];  // dal link di /link: /start Obbligazionario_Copertura
    if (reparti) return gestisciIscrizione("/iscrivimi", `/iscrivimi ${reparti.replace(/_/g, " ")}`, messaggio, env);
    if (attivata) return;
  }
  if (["/start", "/aiuto", "/help"].includes(comando)) {
    return telegram(env, "sendMessage", { chat_id: chat, text: proprietario ? AIUTO : AIUTO_MEMBRI });
  }
  // iscriversi è sempre permesso a tutti, anche con ACCESSO = "proprietario"
  if (["/iscrivimi", "/disiscrivimi", "/iscrizioni"].includes(comando)) {
    return gestisciIscrizione(comando, testo, messaggio, env);
  }
  if (attivata) return;
  // ACCESSO (wrangler.toml): "proprietario" = solo chi gestisce il bot; "tutti" = chiunque lo avvii.
  // Le modifiche e le domande all'archivio restano comunque del proprietario.
  if (!proprietario) {
    return telegram(env, "sendMessage", { chat_id: chat, text: AIUTO_MEMBRI });
  }
  if (["/iscrivi", "/disiscrivi", "/link"].includes(comando)) {
    return gestisciIscrizioneTeam(comando, testo, env, chat);
  }
  await accoda(env, testo);
  const partito = await avviaAgente(env);
  await telegram(env, "sendMessage", {
    chat_id: chat,
    text: partito
      ? `⏳ Ricevuto: «${testo}». Ci lavoro e ti rispondo entro un paio di minuti.`
      : `⏳ Ricevuto: «${testo}». Lo eseguo al prossimo giro (entro un'ora).`,
  });
}

async function gestisciPulsante(pulsante, env) {
  const dati = pulsante.data || "";

  if (dati.startsWith("v+") || dati.startsWith("v-")) {
    const id = dati.slice(2);
    const voto = dati[1] === "+" ? 1 : -1;
    const voti = await leggi(env, "voti", {});
    voti[id] = voti[id] || {};
    voti[id][await anonimo(pulsante.from.id)] = voto;  // vale l'ultimo voto di ciascuno
    await env.STATO.put("voti", JSON.stringify(voti));

    const valori = Object.values(voti[id]);
    const su = valori.filter((v) => v > 0).length;
    const giu = valori.filter((v) => v < 0).length;
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: pulsante.id,
      text: voto > 0 ? "Voto registrato 👍" : "Voto registrato 👎",
    });
    if (pulsante.message) {
      const link = (pulsante.message.reply_markup?.inline_keyboard || [])
        .filter((riga) => !riga.some((tasto) => tasto.callback_data));
      await telegram(env, "editMessageReplyMarkup", {
        chat_id: pulsante.message.chat.id,
        message_id: pulsante.message.message_id,
        reply_markup: { inline_keyboard: [...link, rigaVoti(id, su, giu)] },
      });
    }
    return;
  }

  if (dati.startsWith("p+") || dati.startsWith("p-")) {
    if (String(pulsante.from.id) !== String(env.OWNER_ID)) {
      return telegram(env, "answerCallbackQuery", {
        callback_query_id: pulsante.id,
        text: "Solo chi gestisce il bot può decidere.",
      });
    }
    const applica = dati[1] === "+";
    await accoda(env, `/proposta ${applica ? "applica" : "ignora"} ${dati.slice(2)}`);
    await avviaAgente(env);
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: pulsante.id,
      text: applica ? "Applico la modifica…" : "Proposta ignorata",
    });
    if (pulsante.message) {
      await telegram(env, "editMessageReplyMarkup", {
        chat_id: pulsante.message.chat.id,
        message_id: pulsante.message.message_id,
        reply_markup: { inline_keyboard: [] },
      });
    }
  }
}

// /iscrivimi Obbligazionario Copertura [subito|sera] · /disiscrivimi · /iscrizioni
async function gestisciIscrizione(comando, testo, messaggio, env) {
  const chat = messaggio.chat.id;
  const utente = String(messaggio.from.id);
  const iscrizioni = await leggi(env, "iscrizioni", {});
  const rispondi = (text) => telegram(env, "sendMessage", { chat_id: chat, text });

  if (comando === "/disiscrivimi") {
    delete iscrizioni[utente];
    await env.STATO.put("iscrizioni", JSON.stringify(iscrizioni));
    return rispondi("Fatto: non ti mando più le notizie in privato. Restano tutte sul canale.");
  }
  if (comando === "/iscrizioni" && utente === String(env.OWNER_ID)) {
    return rispondi(await elencoIscritti(env));
  }
  if (comando === "/iscrizioni") {
    const mia = iscrizioni[utente];
    return rispondi(mia
      ? `Sei iscritto a: ${mia.reparti.join(", ")}. Le ricevi ${mia.modo === "sera" ? "ogni sera alle 22 in un unico messaggio" : "subito, una per una"}.`
      : "Non sei iscritto a nessun reparto. Esempio: /iscrivimi Obbligazionario Copertura");
  }

  const { reparti, modo } = leggiReparti(testo.split(/[\s,]+/).slice(1));
  if (reparti.size === 0) {
    return rispondi(`Indica uno o più reparti: ${REPARTI.join(", ")} (oppure "tutti"), e aggiungi "sera" se preferisci un solo riepilogo serale.\nEsempio: /iscrivimi Obbligazionario Copertura sera`);
  }
  iscrizioni[utente] = {
    reparti: [...reparti], modo, chat, nome: messaggio.from.first_name || "", username: (messaggio.from.username || "").toLowerCase(),
  };
  await env.STATO.put("iscrizioni", JSON.stringify(iscrizioni));
  return rispondi(`✅ Iscritto a: ${[...reparti].join(", ")}.\n${modo === "sera"
    ? "Ogni sera alle 22 ti mando in privato le notizie del giorno di questi reparti."
    : "Ti mando in privato ogni notizia di questi reparti, appena esce."}\nPer cambiare: /iscrivimi di nuovo · per smettere: /disiscrivimi`);
}

// Reparti e modo dalle parole dopo il comando: "Obbligazionario", "#RiskManagement", "fx", "tutti", "sera"
function leggiReparti(parole) {
  parole = parole.map((p) => p.toLowerCase().replace(/[#_-]/g, ""));
  const reparti = new Set();
  for (const parola of parole) {
    if (parola === "tutti" || parola === "tutto") REPARTI.forEach((r) => reparti.add(r));
    const reparto = REPARTI.find((r) => r.toLowerCase().replace(/\s/g, "") === parola) || SINONIMI[parola];
    if (reparto) reparti.add(reparto);
  }
  // "risk management" scritto in due parole
  if (parole.includes("risk") || parole.includes("management")) reparti.add("Risk management");
  return { reparti, modo: parole.includes("sera") ? "sera" : "subito" };
}

// Solo il proprietario: /iscrivi @utente <reparti> [sera] · /disiscrivi @utente · /link <reparti>
// L'utente si indica con lo username (@mario) o con il numero di utente Telegram.
async function gestisciIscrizioneTeam(comando, testo, env, chat) {
  const rispondi = (text) => telegram(env, "sendMessage", { chat_id: chat, text });
  const [, chi, ...resto] = testo.split(/[\s,]+/);

  if (comando === "/link") {
    const { reparti } = leggiReparti([chi, ...resto].filter(Boolean));
    if (reparti.size === 0) return rispondi(`Esempio: /link Obbligazionario Copertura\nReparti: ${REPARTI.join(", ")}`);
    const bot = await (await telegram(env, "getMe", {})).json();
    const parametro = [...reparti].map((r) => r.replace(/\s/g, "")).join("_");
    return rispondi(`Manda questo link al team (o a chi serve): chi lo apre e preme Avvia riceve in privato `
      + `le notizie di ${[...reparti].join(", ")}.\n\nhttps://t.me/${bot.result.username}?start=${parametro}`);
  }

  const chiave = (chi || "").replace(/^@/, "").toLowerCase();
  if (!chiave) {
    return rispondi(comando === "/iscrivi"
      ? "Esempio: /iscrivi @mario Obbligazionario Copertura (aggiungi \"sera\" per il solo riepilogo serale)"
      : "Esempio: /disiscrivi @mario");
  }
  const iscrizioni = await leggi(env, "iscrizioni", {});
  const preiscrizioni = await leggi(env, "preiscrizioni", {});
  const attiva = Object.keys(iscrizioni).find((id) => id === chiave || (iscrizioni[id].username || "") === chiave);

  if (comando === "/disiscrivi") {
    if (attiva) delete iscrizioni[attiva];
    const inAttesa = chiave in preiscrizioni;
    delete preiscrizioni[chiave];
    await env.STATO.put("iscrizioni", JSON.stringify(iscrizioni));
    await env.STATO.put("preiscrizioni", JSON.stringify(preiscrizioni));
    return rispondi(attiva || inAttesa ? `Fatto: ${chi} non riceve più le notizie in privato.` : `${chi} non era iscritto.`);
  }

  const { reparti, modo } = leggiReparti(resto);
  if (reparti.size === 0) return rispondi(`Indica i reparti: ${REPARTI.join(", ")} (oppure "tutti").`);
  if (attiva) {  // ha già avviato il bot: l'iscrizione vale subito
    iscrizioni[attiva] = { ...iscrizioni[attiva], reparti: [...reparti], modo };
    await env.STATO.put("iscrizioni", JSON.stringify(iscrizioni));
    await telegram(env, "sendMessage", { chat_id: iscrizioni[attiva].chat, text: avvisoIscrizione(reparti, modo) });
    return rispondi(`✅ ${chi} iscritto a: ${[...reparti].join(", ")}. Gliel'ho comunicato in privato.`);
  }
  preiscrizioni[chiave] = { reparti: [...reparti], modo, quando: new Date().toISOString() };
  await env.STATO.put("preiscrizioni", JSON.stringify(preiscrizioni));
  const bot = await (await telegram(env, "getMe", {})).json();
  return rispondi(`✅ ${chi} iscritto a: ${[...reparti].join(", ")}.\n`
    + `Telegram non permette al bot di scrivere a chi non l'ha mai avviato: le notizie gli arrivano appena `
    + `apre @${bot.result.username} e preme Avvia (anche dal link https://t.me/${bot.result.username}).`);
}

// /argomenti nel gruppo del team (solo il proprietario): un argomento per reparto, ricordato nel KV.
// Ripeterlo crea solo quelli che mancano. Gli altri messaggi (buongiorno, chiusura...) restano in Generale.
async function creaArgomenti(messaggio, proprietario, env) {
  const chat = messaggio.chat.id;
  const rispondi = (text) => telegram(env, "sendMessage", {
    chat_id: chat, text, ...(messaggio.is_topic_message ? { message_thread_id: messaggio.message_thread_id } : {}),
  });
  if (!proprietario) {
    return rispondi("Solo chi gestisce il bot può creare gli argomenti. Se scrivi come amministratore anonimo, "
      + "disattiva «Resta anonimo» nei tuoi permessi di amministratore e riprova.");
  }
  if (!messaggio.chat.is_forum) {
    return rispondi("Prima attiva gli argomenti: info del gruppo → Modifica → Argomenti. Poi riscrivi /argomenti.");
  }
  const salvato = await leggi(env, "gruppo", {});
  const argomenti = salvato.chat === chat ? { ...salvato.argomenti } : {};
  const errori = [];
  for (const [i, reparto] of REPARTI.entries()) {
    if (argomenti[reparto]) continue;
    const r = await (await telegram(env, "createForumTopic", {
      chat_id: chat, name: reparto, icon_color: COLORI_ARGOMENTI[i % COLORI_ARGOMENTI.length],
    })).json();
    if (r.ok) argomenti[reparto] = r.result.message_thread_id;
    else errori.push(r.description);
  }
  await env.STATO.put("gruppo", JSON.stringify({ chat, argomenti }));
  if (errori.length) {
    return rispondi(`Non sono riuscito a creare ${errori.length} argomenti (${errori[0]}).\n`
      + "Controlla che io sia amministratore del gruppo con il permesso «Gestisci argomenti», poi riscrivi /argomenti.");
  }
  return rispondi(`✅ Argomenti pronti: ${REPARTI.join(", ")}.\n`
    + "Dal prossimo giro pubblico qui ogni notizia nell'argomento dei suoi reparti, non più nel canale. "
    + "Buongiorno, chiusura e riepilogo della settimana arrivano in Generale.\n"
    + "Ognuno può silenziare gli argomenti che non segue: entra nell'argomento → nome in alto → Disattiva notifiche.");
}

// /reparto Macroeconomia (o più reparti) in un gruppo, solo il proprietario: l'agente manda a quel gruppo
// le notizie e i dati di quei reparti. Ogni reparto ha un solo gruppo; /reparto nessuno lo scollega.
async function assegnaReparto(messaggio, proprietario, testo, env) {
  const chat = messaggio.chat.id;
  const rispondi = (text) => telegram(env, "sendMessage", {
    chat_id: chat, text, ...(messaggio.is_topic_message ? { message_thread_id: messaggio.message_thread_id } : {}),
  });
  if (!proprietario) {
    return rispondi("Solo chi gestisce il bot può collegare un gruppo a un reparto. Se scrivi come amministratore "
      + "anonimo, disattiva «Resta anonimo» nei tuoi permessi di amministratore e riprova.");
  }
  const parole = testo.split(/[\s,]+/).slice(1);
  const gruppi = await leggi(env, "gruppi_reparti", {});
  for (const reparto of Object.keys(gruppi)) {
    if (gruppi[reparto].chat === chat) delete gruppi[reparto];  // il gruppo riceve solo i reparti indicati ora
  }
  if (parole.some((p) => p.toLowerCase() === "nessuno")) {
    await env.STATO.put("gruppi_reparti", JSON.stringify(gruppi));
    return rispondi("Fatto: questo gruppo non riceve più le notizie dei reparti.");
  }
  const { reparti } = leggiReparti(parole);
  if (reparti.size === 0) {
    return rispondi(`Scrivi il reparto di questo gruppo, es. /reparto Macroeconomia\nReparti: ${REPARTI.join(", ")}`);
  }
  for (const reparto of reparti) gruppi[reparto] = { chat, nome: messaggio.chat.title || "" };
  await env.STATO.put("gruppi_reparti", JSON.stringify(gruppi));
  return rispondi(`✅ Questo gruppo riceve le notizie di: ${[...reparti].join(", ")}.\n`
    + "Le mando qui dal prossimo giro, insieme ai dati economici di questi reparti. Il canale resta com'è, "
    + "con tutte le notizie, il buongiorno e i riepiloghi.");
}

// Al primo messaggio di un membro iscritto d'ufficio: l'iscrizione diventa attiva con la sua chat
async function attivaPreiscrizione(messaggio, env) {
  const preiscrizioni = await leggi(env, "preiscrizioni", {});
  const username = (messaggio.from.username || "").toLowerCase();
  const chiave = [String(messaggio.from.id), username].find((k) => k && k in preiscrizioni);
  if (!chiave) return false;
  const { reparti, modo } = preiscrizioni[chiave];
  const iscrizioni = await leggi(env, "iscrizioni", {});
  iscrizioni[String(messaggio.from.id)] = {
    reparti, modo, chat: messaggio.chat.id, nome: messaggio.from.first_name || "", username,
  };
  delete preiscrizioni[chiave];
  await env.STATO.put("iscrizioni", JSON.stringify(iscrizioni));
  await env.STATO.put("preiscrizioni", JSON.stringify(preiscrizioni));
  await telegram(env, "sendMessage", { chat_id: messaggio.chat.id, text: avvisoIscrizione(new Set(reparti), modo) });
  return true;
}

function avvisoIscrizione(reparti, modo) {
  return `✅ Sei stato iscritto alle notizie di: ${[...reparti].join(", ")}.\n${modo === "sera"
    ? "Ogni sera alle 22 ti mando in privato le notizie del giorno di questi reparti."
    : "Ti mando in privato ogni notizia di questi reparti, appena esce."}\n`
    + "Per cambiare: /iscrivimi <reparti> · per smettere: /disiscrivimi";
}

async function elencoIscritti(env) {
  const iscrizioni = Object.values(await leggi(env, "iscrizioni", {}));
  const preiscrizioni = Object.entries(await leggi(env, "preiscrizioni", {}));
  const righe = iscrizioni.map((i) => `• ${i.nome || ""}${i.username ? ` @${i.username}` : ""}: `
    + `${i.reparti.join(", ")}${i.modo === "sera" ? " (sera)" : ""}`);
  const attesa = preiscrizioni.map(([chi, i]) => `• @${chi}: ${i.reparti.join(", ")} – non ha ancora avviato il bot`);
  const gruppi = Object.entries(await leggi(env, "gruppi_reparti", {}))
    .map(([reparto, g]) => `• ${reparto}: ${g.nome || g.chat}`);
  if (!righe.length && !attesa.length && !gruppi.length) {
    return "Nessun iscritto. Per iscrivere qualcuno: /iscrivi @utente <reparti>";
  }
  return [righe.length ? `Iscritti (${righe.length}):\n${righe.join("\n")}` : "",
          attesa.length ? `In attesa (${attesa.length}):\n${attesa.join("\n")}` : "",
          gruppi.length ? `Gruppi dei reparti:\n${gruppi.join("\n")}` : ""].filter(Boolean).join("\n\n");
}

function rigaVoti(id, su, giu) {
  return [
    { text: su ? `👍 ${su}` : "👍", callback_data: `v+${id}` },
    { text: giu ? `👎 ${giu}` : "👎", callback_data: `v-${id}` },
  ];
}

// Stesso calcolo di feedback.anonimo in Python: sha256("voto-<id>"), primi 12 caratteri
async function anonimo(idUtente) {
  const impronta = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`voto-${idUtente}`));
  return [...new Uint8Array(impronta)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 12);
}

async function accoda(env, testo) {
  const coda = await leggi(env, "coda", []);
  coda.push({ id: crypto.randomUUID(), testo, quando: new Date().toISOString() });
  await env.STATO.put("coda", JSON.stringify(coda));
}

// Fa partire subito l'agente su GitHub; senza token il comando aspetta il giro programmato
async function avviaAgente(env, motivo = "comando") {
  if (!env.GITHUB_TOKEN) return false;
  const risposta = await fetch(`https://api.github.com/repos/${env.REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "agente-notizie-bot",
    },
    body: JSON.stringify({ event_type: motivo }),
  });
  return risposta.status === 204;
}

async function leggi(env, chiave, vuoto) {
  return JSON.parse((await env.STATO.get(chiave)) || JSON.stringify(vuoto));
}

function telegram(env, metodo, corpo) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${metodo}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(corpo),
  });
}

function json(dati) {
  return new Response(JSON.stringify(dati), { headers: { "Content-Type": "application/json" } });
}
