# Sécurité

Le modèle de sécurité et ses limites sont détaillés dans [SECURITY.txt](SECURITY.txt).
Le profil actuellement installé et relu par Matter est 40005. Il conserve les
protections de 40002 ; la portée des essais matériels et les limites restantes
sont consignées dans VERIFICATION.txt et PRODUCTION.txt.

## Signaler une vulnérabilité en privé

Écrire au mainteneur à **[pmloikju@pm.me](mailto:pmloikju@pm.me)**. Ne pas ouvrir
une issue publique pour une vulnérabilité non corrigée ou une fuite de données.
Pour chiffrer un rapport sensible, utiliser la
[clé publique OpenPGP du contact](docs/security-contact.asc).

Empreinte de la clé principale :

```text
D05A BA3B B6BA 5D8D AC5B  8B2C 0A1C 4D03 B27A 0809
```

La clé fournie par le mainteneur a été inspectée le 11 novembre 2026 : identité
`pmloikju@pm.me`, clé principale Ed25519 et sous-clé de chiffrement cv25519.
Seule la partie publique est distribuée. Pour une vérification indépendante,
confirmer l'empreinte auprès du mainteneur par un autre canal de confiance.

Fournir version du firmware, empreinte de l'image, impact et reproduction
minimale, sans PIN, clé privée, token, fabric, NVS ou capture de commissioning.
Aucun délai de réponse garanti n'est annoncé.

Les rapports de fonctionnement peuvent utiliser un résumé anonymisé. Les
captures et sauvegardes complètes de flash doivent rester privées.
