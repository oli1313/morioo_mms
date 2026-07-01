// Firmware Wemos D1 Mini — DEUX relais adressés indépendamment.
//
// Protocole série (115200 baud) : commandes de 2 caractères « <canal><état> »
//   P1 / P0  → relais POMPE de cale  ON / OFF
//   L1 / L0  → relais FEUX sous-marins ON / OFF
//
// Le préfixe de canal évite l'ambiguïté de l'ancien protocole (un seul '1'/'0'
// pilotait un unique relais partagé entre pompe et feux — ils s'écrasaient).
// Le Pi (main.py `_send_relay(device, state)`) envoie toujours les 2 octets.

const int PUMP_PIN   = D1;   // relais pompe de cale
const int LIGHTS_PIN = D2;   // relais feux sous-marins

char pendingChannel = 0;     // 'P' ou 'L' reçu, en attente de l'état '1'/'0'

void setup() {
  Serial.begin(115200);            // Communication USB
  pinMode(PUMP_PIN, OUTPUT);
  pinMode(LIGHTS_PIN, OUTPUT);
  digitalWrite(PUMP_PIN, LOW);     // Éteints par défaut
  digitalWrite(LIGHTS_PIN, LOW);
}

void loop() {
  if (Serial.available() > 0) {
    char c = Serial.read();

    if (c == 'P' || c == 'L') {
      pendingChannel = c;                       // mémorise le canal visé
    }
    else if ((c == '1' || c == '0') && pendingChannel) {
      int pin = (pendingChannel == 'P') ? PUMP_PIN : LIGHTS_PIN;
      digitalWrite(pin, c == '1' ? HIGH : LOW);
      Serial.print("OK:");
      Serial.print(pendingChannel);
      Serial.println(c == '1' ? "ON" : "OFF");
      pendingChannel = 0;                        // commande consommée
    }
    // Tout autre octet (ou un '1'/'0' sans canal préalable) est ignoré :
    // pas de relais actionné par erreur sur un parasite série.
  }
}
