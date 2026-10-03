/**
 * Worker di Cloudflare per il bot Telegram dell'agente notizie.
 *
 * Telegram consegna qui ogni aggiornamento del bot (webhook), così il bot risponde subito:
 * - voti 👍/👎 sotto le notizie: li registra in forma anonima e aggiorna il conteggio sui pulsanti;
 * - messaggi del proprietario: li mette in coda come comandi e fa partire subito l'agente su GitHub;
 * - pulsanti della proposta di profilo: diventano il comando /proposta applica|ignora <id>.
 * L'agente (agente.py) a ogni giro ritira da qui voti e comandi (/agente/voti, /agente/coda).
 * Fa anche da orologio: ogni 15 minuti avvia l'agente su GitHub (vedi "scheduled").
 *
 * Segreti (npx wrangler secret put): TELEGRAM_TOKEN, WEBHOOK_SECRET, AGENTE_KEY, OWNER_ID, GITHUB_TOKEN.
 * Variabile in wrangler.toml: REPO. Archivio: KV con nome "STATO".
 */

const AIUTO = `Comandi del bot (oppure scrivimi in italiano normale, es. "segui anche Mediobanca"):

/segui <società> – aggiungo un titolo da seguire
/tema <argomento> – aggiungo un tema da seguire
/smetti <nome> – smetto di seguire un titolo o un tema
/soglia <1-10> – cambio la soglia di rilevanza (più bassa = più notizie)
/profilo <frase> – aggiungo un'indicazione al profilo
/notizie – faccio subito un giro di notizie
/stato – com'è andata oggi
/aiuto – questo elenco`;

export default {
  // Orologio: ogni 15 minuti fa partire l'agente su GitHub (orari in wrangler.toml, in UTC).
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
  if (!messaggio || messaggio.chat.type !== "private" || !messaggio.text) return;
  const chat = messaggio.chat.id;
  if (String(messaggio.from.id) !== String(env.OWNER_ID)) {
    return telegram(env, "sendMessage", {
      chat_id: chat,
      text: "Ciao! Questo bot pubblica le notizie del canale del team WhiteRock. I comandi sono riservati a chi lo gestisce.",
    });
  }

  const testo = messaggio.text.trim();
  if (["/start", "/aiuto", "/help"].includes(testo.split(/[\s@]/)[0].toLowerCase())) {
    return telegram(env, "sendMessage", { chat_id: chat, text: AIUTO });
  }
  await accoda(env, testo);
  const partito = await avviaAgente(env);
  await telegram(env, "sendMessage", {
    chat_id: chat,
    text: partito
      ? `⏳ Ricevuto: «${testo}». Ci lavoro e ti rispondo entro un paio di minuti.`
      : `⏳ Ricevuto: «${testo}». Lo eseguo al prossimo giro (entro un quarto d'ora).`,
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
