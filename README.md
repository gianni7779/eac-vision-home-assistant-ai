# EAC Vision AI per Home Assistant su mini PC

Repository separato per installare **EAC Vision AI Home Assistant** dallo Store delle app di Home Assistant.

> **Solo mini PC x86-64 (`amd64`) con Home Assistant OS o Supervised.** Questa edizione esegue il modello AI sulla CPU del mini PC. Per Raspberry Pi 4 usa [EAC Vision senza AI](https://github.com/gianni7779/eac-vision-home-assistant). Non installare entrambe le edizioni sullo stesso HA registrando le stesse telecamere: creerebbero due registrazioni e due flussi di analisi.

## Installazione

1. In Home Assistant apri **Impostazioni → App → Store → ⋮ → Repository**.
2. Aggiungi `https://github.com/gianni7779/eac-vision-home-assistant-ai`.
3. Installa **EAC Vision AI Home Assistant** e avviala.
   Se è già installata, aggiorna il repository dallo Store e poi l'app alla versione **0.1.10**.
4. Aggiungi prima le camere all'integrazione **ONVIF** di Home Assistant; EAC Vision le importerà automaticamente.
5. Monta disco o NAS sotto `/media`, apri la dashboard e imposta la cartella dell'archivio.

## Funzioni

- Dashboard delle camere selezionate, con fotogramma completo, vista singola, audio compatibile e schermo intero.
- Live condiviso tramite go2rtc e registrazione continua H24 senza ricodificare il video. Se il profilo ONVIF della telecamera fornisce audio, viene incluso nei nuovi file MP4 in AAC; le impostazioni mostrano se l'ultima registrazione contiene la traccia audio. Timeline giornaliera con anteprima video, audio, velocità 0,5×–4× e zoom. I segmenti si chiudono ogni circa 30 secondi; la durata esatta dipende dai fotogrammi chiave della telecamera. Il collegamento dagli eventi aspetta il segmento corretto e lo apre automaticamente.
- **YOLOX Tiny**, lo stesso modello ONNX della versione PC: riconosce persone, veicoli e animali dopo un controllo del movimento. Sensibilità e categorie regolabili per camera.
- Eventi AI con istantanea, filtro per camera/tipo, segni colorati sulla barra delle registrazioni (persone arancione, animali verde, veicoli viola), collegamento alla registrazione, evento `eac_vision_ai` nel bus di Home Assistant per le automazioni e API compatibile con i client EAC Vision. Il modello è elaborato sulla CPU; il numero di camere AI contemporanee parte da 4 ed è configurabile fino a 16 in base alla potenza effettiva del mini PC.
- Notifiche per persone, animali e veicoli anche con EAC Vision chiusa: nelle app iPhone/Android scegli il dispositivo `notify.mobile_app_*` di Home Assistant Companion, i tipi di evento e prova l'invio. Le notifiche arrivano tramite l'app Home Assistant installata sul telefono.
- Registrazioni cancellate automaticamente: 3 giorni sulle nuove installazioni, pulizia ogni 5 minuti, limite GB facoltativo e riserva di spazio sul disco/NAS.
- Impostazioni con spazio occupato, memoria e CPU del servizio; codice client breve o personalizzato; nomi delle camere nascondibili nel live.
- Collegamento dei client da casa con IP locale/porta 8767, o fuori casa tramite Tailscale. DuckDNS richiede un proxy HTTPS che inoltri `/eacnvr/` e i WebSocket.

L'AI non modifica i video H24. L'analisi usa fotogrammi 640×360 a 5 fps, con controllo del movimento e un solo lavoratore ONNX Runtime. Se ONVIF offre un profilo secondario di almeno 640×360 e non oltre 1280×720, l'AI lo prova e usa quello; live e registrazione H24 rimangono sul profilo principale. Se il profilo secondario fallisce o la registrazione segnala un errore, l'AI torna automaticamente al principale. Il riquadro di ogni camera mostra il profilo usato, i fotogrammi ricevuti e le analisi effettuate. Se la memoria disponibile scende sotto 768 MB l'analisi si sospende temporaneamente. La velocità e il numero di camere sostenibili dipendono dal mini PC, dai codec, dal bitrate e dal NAS. La 0.1.7 ha riconosciuto una persona durante una prova reale su Home Assistant con tre camere; la stabilità prolungata e il guadagno del profilo secondario vanno ancora verificati sul dispositivo.

La pipeline AI viene provata automaticamente su un [video campione di OpenCV con persone](https://github.com/opencv/opencv/blob/master/samples/data/vtest.avi): il test attraversa rilevamento del movimento, modello ONNX e creazione dell'evento `persona`. Nelle Impostazioni AI, per ogni camera, i contatori mostrano fotogrammi, analisi e persone nel fotogramma; "In attesa di movimento" indica che il video arriva ma il modello non è ancora stato chiamato. Questo test non sostituisce la prova sul flusso della tua telecamera.

Gli eventi vengono conservati almeno 7 giorni nei dati dell'app, mentre le registrazioni seguono i giorni impostati per l'archivio. Il modello e la licenza Apache 2.0 di YOLOX sono inclusi in `eac_vision_ai/ai/`.

## Collegamento alle app

In **Impostazioni → PC, TV e telefono** copia il codice client e inserisci nell'app l'indirizzo del mini PC HA e la porta **8767**. Su Tailscale usa l'IP `100.x.y.z` del mini PC. Il percorso `/eacnvr/info` risponde senza codice per verificare che il servizio sia raggiungibile. Non aprire la porta 8767 direttamente su Internet.

L'edizione AI ha uno slug diverso da quella senza AI e conserva impostazioni proprie. I file video dell'altra edizione non vengono copiati automaticamente.
