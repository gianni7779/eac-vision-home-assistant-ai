# EAC Vision AI Home Assistant

Edizione per **mini PC amd64**. Importa le camere dall'integrazione ONVIF di Home Assistant, mostra il live e registra H24 su disco o NAS montato sotto `/media`. La dashboard mantiene il fotogramma intero; clicca una camera per audio, schermo intero e registrazioni. Nella scheda **Eventi AI** trovi persone, veicoli e animali rilevati con istantanea e collegamento al video di quell'ora.

Nelle **Impostazioni → Rilevamento AI** puoi attivare/disattivare l'analisi, scegliere quante telecamere analizzare contemporaneamente (inizialmente 4), regolare sensibilità e categorie per ogni camera. L'AI usa il modello YOLOX Tiny della versione PC sulla CPU e analizza solo quando c'è movimento. Prova prima con una camera sul tuo mini PC e controlla CPU, memoria e temperatura prima di aumentare il numero.

Per l'archivio scegli una cartella già esistente sotto `/media`. Le nuove installazioni conservano 3 giorni di video e verificano ogni 5 minuti i file più vecchi. Il limite GB è facoltativo; l'app cerca comunque di riservare almeno il 5% del disco (minimo 2 GB) e non usa più dell'80% della capacità totale per le proprie registrazioni. Il segmento ancora in scrittura non viene cancellato.

Nelle impostazioni trovi anche uso di CPU/memoria del servizio, spazio usato dai video, codice per collegare PC/TV/telefono modificabile e interruttore per togliere i nomi delle camere dalla griglia live. Se cambi il codice, aggiornalo nei client già collegati.

Per usare i client fuori casa con Tailscale, installa Tailscale sia sul mini PC HA sia sul dispositivo da cui guardi; inserisci nell'app l'IP Tailscale `100.x.y.z` del mini PC, porta 8767 e il codice client. DuckDNS da solo apre Home Assistant ma non inoltra l'API NVR: serve un proxy HTTPS che inoltri `/eacnvr/` e i WebSocket. Non pubblicare la porta 8767 direttamente su Internet.

Questa edizione è separata da quella senza AI per Raspberry Pi. Le due app hanno dati di configurazione separati. Evita di registrare le stesse camere con entrambe sullo stesso Home Assistant.
