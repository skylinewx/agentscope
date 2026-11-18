/*
 * @(#)NvwaTokenDTO.java  
 */
package com.jiuqi.nvwa.certification.dto;

import java.io.Serializable;

import com.jiuqi.nvwa.intergration.sdk.cer.dto.NvwaUserDTO;

/**
 *  女娲 申请 ticket的结果
 *<p>Title:</p>
 * @author: lixihao
 * @date: 2021/03/22
 * @version: v1.0
 */
public class NvwaVerityTicketDTO implements Serializable{

	/**
	 * 
	 */
	private static final long serialVersionUID = 1L;
	/**
	 * 唯一标识
	 */
	private String id;
	
	/**
	 * 关联的用户信息
	 */
	private NvwaUserDTO user;
	
	
	public String getId() {
		return id;
	}
	public NvwaUserDTO getUser() {
		return user;
	}
	public void setId(String id) {
		this.id = id;
	}
	public void setUser(NvwaUserDTO user) {
		this.user = user;
	}
	
}
