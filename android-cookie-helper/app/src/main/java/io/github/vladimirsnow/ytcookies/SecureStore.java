package io.github.vladimirsnow.ytcookies;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Export remains inside the app sandbox, encrypted using a device-bound key. */
final class SecureStore {
    private static final String ALIAS = "yt_cookie_export_v1";
    private static SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore"); store.load(null);
        if (store.containsAlias(ALIAS)) return (SecretKey) store.getKey(ALIAS, null);
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).build());
        return generator.generateKey();
    }
    static void save(Context context, String netscape) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        byte[] encrypted = cipher.doFinal(netscape.getBytes(StandardCharsets.UTF_8));
        boolean saved = context.getSharedPreferences("export", Context.MODE_PRIVATE).edit()
                .putString("iv", Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP))
                .putString("data", Base64.encodeToString(encrypted, Base64.NO_WRAP))
                .putLong("savedAt", System.currentTimeMillis()).commit();
        if (!saved) throw new IllegalStateException("Не удалось сохранить локальную копию.");
    }
    static String load(Context context) throws Exception {
        var prefs = context.getSharedPreferences("export", Context.MODE_PRIVATE);
        String data = prefs.getString("data", null);
        if (data == null) return null;
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128,
                Base64.decode(prefs.getString("iv", ""), Base64.NO_WRAP)));
        return new String(cipher.doFinal(Base64.decode(data, Base64.NO_WRAP)), StandardCharsets.UTF_8);
    }
    static void clear(Context context) {
        context.getSharedPreferences("export", Context.MODE_PRIVATE).edit().clear().commit();
        try { KeyStore store = KeyStore.getInstance("AndroidKeyStore");store.load(null);store.deleteEntry(ALIAS); }
        catch (Exception ignored) { }
    }
}
