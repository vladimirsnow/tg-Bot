package io.github.vladimirsnow.ytcookies;

import android.content.Intent;
import android.content.Context;
import android.os.SystemClock;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.uiautomator.By;
import androidx.test.uiautomator.UiDevice;
import androidx.test.uiautomator.Until;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.mozilla.geckoview.WebExtension;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.concurrent.atomic.AtomicReference;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import android.net.Uri;
import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public class ExportFlowTest {
    @Test public void fileImportKeepsOnlyYoutubeAndProducesRenderEnv() throws Exception {
        var instrumentation=InstrumentationRegistry.getInstrumentation();
        Context context=instrumentation.getTargetContext();SecureStore.clear(context);
        MainActivity activity=(MainActivity)instrumentation.startActivitySync(
                new Intent(context,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_CLEAR_TASK));
        File input=new File(context.getFilesDir(),"fixture-cookies.txt");
        String cookies="# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\ttest-session\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\ttest-api\n.google.com\tTRUE\t/\tTRUE\t2147483647\tFOREIGN\tignored\n";
        try(FileOutputStream output=new FileOutputStream(input)){output.write(cookies.getBytes(StandardCharsets.UTF_8));}
        instrumentation.runOnMainSync(()->activity.onActivityResult(10,android.app.Activity.RESULT_OK,new Intent().setData(Uri.fromFile(input))));
        UiDevice device=UiDevice.getInstance(instrumentation);
        assertTrue(device.wait(Until.hasObject(By.text("Копировать для Render")),15000));
        CookieCodec.Export exported=CookieCodec.parse(SecureStore.load(context));
        assertEquals(2,exported.count);assertFalse(exported.netscape.contains("FOREIGN"));
        try(FileOutputStream output=new FileOutputStream(new File(context.getFilesDir(),"verified-export.env"))){output.write(exported.env().getBytes(StandardCharsets.UTF_8));}
        instrumentation.runOnMainSync(activity::finish);
    }

    @Test public void browserCookieBridgeExportsAndEncryptedCopySurvivesActivityRestart() throws Exception {
        var instrumentation=InstrumentationRegistry.getInstrumentation();
        Context context=instrumentation.getTargetContext();SecureStore.clear(context);
        UiDevice device=UiDevice.getInstance(instrumentation);
        Intent start=new Intent(context,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_CLEAR_TASK);
        MainActivity activity=(MainActivity)instrumentation.startActivitySync(start);
        assertTrue(device.wait(Until.hasObject(By.text("Новая сессия YouTube")),15000));
        device.findObject(By.text("Новая сессия YouTube")).click();
        Field field=MainActivity.class.getDeclaredField("port");field.setAccessible(true);
        long end=SystemClock.elapsedRealtime()+60000;
        while(field.get(null)==null && SystemClock.elapsedRealtime()<end)SystemClock.sleep(300);
        assertNotNull("Native cookie extension did not connect",field.get(null));
        // Give session initialization/clearData time to finish before planting fixtures.
        SystemClock.sleep(2500);
        instrumentation.runOnMainSync(()->{
            try {JSONObject command=new JSONObject();command.put("action","seed_test");((WebExtension.Port)field.get(null)).postMessage(command);}
            catch(Exception e){throw new RuntimeException(e);}
        });
        SystemClock.sleep(1200);
        assertTrue(device.wait(Until.hasObject(By.text("Получить cookies")),15000));
        device.findObject(By.text("Получить cookies")).click();
        assertTrue("Export never reached local encrypted storage",device.wait(Until.hasObject(By.text("Копировать для Render")),45000));
        String saved=SecureStore.load(context);assertNotNull(saved);
        CookieCodec.Export exported=CookieCodec.parse(saved);
        assertTrue(exported.count>=2);assertTrue(saved.contains("test-session"));assertTrue(saved.contains("test-api"));
        assertFalse(context.getSharedPreferences("export",Context.MODE_PRIVATE).getString("data","").contains("test-session"));
        Method close=MainActivity.class.getDeclaredMethod("closeSession");close.setAccessible(true);
        instrumentation.runOnMainSync(()->{
            try{close.invoke(activity);activity.finish();}catch(Exception e){throw new RuntimeException(e);}
        });
        MainActivity reopened=(MainActivity)instrumentation.startActivitySync(start);
        assertTrue(device.wait(Until.hasObject(By.text("Копировать для Render")),15000));
        assertFalse(device.hasObject(By.text("Получить cookies")));
        instrumentation.runOnMainSync(reopened::finish);
    }
}
