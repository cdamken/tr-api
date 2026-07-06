<?php
// Decrypt the stored TR PIN for user 'carlos' using ownCloud's ICrypto,
// so a server-side probe can log in without the PIN passing through chat.
// Prints ONLY the decrypted PIN to stdout. Run as www-data.
define('OC_CONSOLE', 1);
require '/var/www/owncloud/lib/base.php';

$config = \OC::$server->getConfig();
$crypto = \OC::$server->getCrypto();

$uid = 'carlos';
$enc = (string) $config->getUserValue($uid, 'trade_republic_next', 'pin_enc', '');
if ($enc === '') {
    $enc = (string) $config->getUserValue($uid, 'trade_republic', 'pin_enc', '');
}
if ($enc === '') {
    fwrite(STDERR, "no pin_enc stored for $uid in either app\n");
    exit(3);
}
echo $crypto->decrypt($enc);
