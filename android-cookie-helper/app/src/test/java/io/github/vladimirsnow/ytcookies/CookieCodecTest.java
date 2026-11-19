package io.github.vladimirsnow.ytcookies;

import org.junit.Test;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.List;
import static org.junit.Assert.*;

public class CookieCodecTest {
    private String rows(String extra) {
        return "# Netscape HTTP Cookie File\n"
            +"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\ttest-session\n"
            +".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\ttest-api\n"+extra;
    }
    @Test public void envRoundTripKeepsHttpOnlyAndDropsOtherDomains() {
        CookieCodec.Export e=CookieCodec.parse(rows(".google.com\tTRUE\t/\tTRUE\t2147483647\tSECRET\tnot-youtube\n"));
        assertEquals(2,e.count);
        assertFalse(e.netscape.contains("not-youtube"));
        assertTrue(e.netscape.contains("#HttpOnly_.youtube.com"));
        assertEquals(e.netscape,new String(Base64.getDecoder().decode(e.base64),StandardCharsets.UTF_8));
        assertEquals("YOUTUBE_COOKIES_BASE64="+e.base64+"\n",e.env());
    }
    @Test public void sessionExpirationZeroBecomesBlankAndIsKept() {
        CookieCodec.Export e=CookieCodec.parse(rows(".youtube.com\tTRUE\t/\tTRUE\t0\tSESSION\tvalue\n"));
        assertEquals(3,e.count);assertTrue(e.netscape.contains("TRUE\t\tSESSION\tvalue"));
    }
    @Test public void expiredCookiesAreExcluded() {
        assertEquals(2,CookieCodec.parse(rows(".youtube.com\tTRUE\t/\tTRUE\t1\tOLD\tvalue\n")).count);
    }
    @Test public void anonymousOrSpoofedDomainCannotProduceAnAuthenticatedExport() {
        for(String text:new String[]{"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tVISITOR\tvalue\n",
                rows("").replace(".youtube.com",".youtube.com.attacker.invalid")}) {
            try{CookieCodec.parse(text);fail("Unauthenticated export accepted");}catch(IllegalArgumentException expected){}
        }
    }
    @Test public void malformedAndOversizedInputAreRejected() {
        for(String text:new String[]{"{\"cookies\": []}", rows("")+"broken line\n", "x".repeat(CookieCodec.MAX_BYTES+1)}) {
            try{CookieCodec.parse(text);fail("Bad input accepted");}catch(IllegalArgumentException expected){}
        }
    }
    @Test public void browserCookieNewlineInjectionIsRejected() {
        try {
            CookieCodec.build(List.of(new CookieCodec.Cookie(".youtube.com","/","SID","a\nb",true,true,true,null)));
            fail("Newline injection accepted");
        }catch(IllegalArgumentException expected){}
    }
}
