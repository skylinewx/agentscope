/*
 * @(#)ClientDecoder.java  
 */
package com.jiuqi.nvwa.certification.authentication.ticket;

import java.nio.charset.Charset;
import java.util.Base64;

public class TicketDecoder {
	
	private String remoteCode;
	private String ticket;
	private final Charset charset;
	private final String authorication;
	
	public TicketDecoder(String authorization, Charset charset) {
		this.charset = charset;
		this.authorication = authorization;
	}
	public boolean tryDecode() {
		try {
			byte[] clientInfoByte = Base64.getDecoder().decode(authorication.getBytes(charset));
			String clientInfoStr = new String(clientInfoByte, charset);
			String[] clientInfo = clientInfoStr.split(":");
			if (clientInfo.length < 2 ) {
				this.remoteCode = "";
				this.ticket = clientInfoStr;	
			} else {
				this.remoteCode = clientInfo[0];
				this.ticket = clientInfo[1];			
			}
			return true;
		} catch (Exception e) {
			return false;
		}
	}
	public String getRemoteCode() {
		return remoteCode;
	}
	public String getTicket() {
		return ticket;
	}
	public void setRemoteCode(String remoteCode) {
		this.remoteCode = remoteCode;
	}
}
