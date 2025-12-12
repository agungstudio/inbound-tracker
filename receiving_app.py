import streamlit as st
import pandas as pd
from supabase import create_client
from datetime import datetime, timezone
import time
import io
import json
import logging
from postgrest.exceptions import APIError
from openpyxl.styles import PatternFill, Font, Alignment

# --- KONFIGURASI [v1.1 - Stable with Logging] ---
SUPABASE_URL = st.secrets.get("SUPABASE_URL")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY")
DAFTAR_CHECKER = ["Agung", "Al Fath", "Reza", "Rico", "Sasa", "Mita", "Koordinator"]
RESET_PIN = "123456" 
SESSION_KEY_CHECKER = "current_checker_name_receiving" 
RECEIVING_TABLE = "receiving_validation" # Nama Tabel di Supabase

# Configure basic logging
logging.basicConfig(level=logging.INFO)

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("⚠️ KONFIGURASI DATABASE BELUM ADA.")
    st.markdown("Harap masukkan `SUPABASE_URL` dan `SUPABASE_KEY` di **Secrets Streamlit Cloud** atau file `.streamlit/secrets.toml`.")
    st.stop()

@st.cache_resource
def init_connection():
    try:
        logging.info("Attempting to connect to Supabase...")
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        # Quick check connection
        client.table(RECEIVING_TABLE).select("id").limit(0).execute()
        return client
    except Exception as e:
        logging.error(f"Failed to connect to Supabase: {e}")
        st.error("❌ KONEKSI DATABASE GAGAL. Pastikan URL dan Kunci Supabase Anda benar.")
        st.stop()

supabase = init_connection()

# --- FUNGSI HELPER WAKTU & KONVERSI ---
def parse_supabase_timestamp(timestamp_str):
    """Mengubah string timestamp Supabase menjadi objek datetime yang aman"""
    try:
        if timestamp_str and timestamp_str.endswith('Z'):
             timestamp_str = timestamp_str[:-1] + '+00:00'
        return datetime.fromisoformat(timestamp_str) if timestamp_str else datetime(1970, 1, 1, tzinfo=timezone.utc)
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

def convert_df_to_excel(df, sheet_name='Data_Receiving'):
    """Mengubah DataFrame menjadi file Excel dengan Header Cantik"""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        cols = ['gr_number', 'sku', 'nama_barang', 'qty_po', 'qty_fisik', 'qty_diff', 'keterangan', 'jenis', 'sn_list', 'updated_by', 'updated_at']
        
        df_export = df.copy()
        if 'sn_list' in df_export.columns:
             df_export['sn_list'] = df_export['sn_list'].apply(lambda x: "; ".join(x) if isinstance(x, list) else (x if pd.notna(x) else ''))
        
        df_export['qty_diff'] = df_export['qty_fisik'] - df_export['qty_po']
        
        available_cols = [c for c in cols if c in df_export.columns]
        df_export = df_export[available_cols] if not df_export.empty else df_export
        
        df_export.to_excel(writer, index=False, sheet_name=sheet_name)
        worksheet = writer.sheets[sheet_name]
        
        blibli_blue_fill = PatternFill(start_color="0095DA", end_color="0095DA", fill_type="solid")
        white_bold_font = Font(color="FFFFFF", bold=True, size=11)
        center_align = Alignment(horizontal='center', vertical='center')
        
        for cell in worksheet[1]:
            cell.fill = blibli_blue_fill
            cell.font = white_bold_font
            cell.alignment = center_align
            
        for column_cells in worksheet.columns:
            length = max(len(str(cell.value) if cell.value else "") for cell in column_cells)
            worksheet.column_dimensions[column_cells[0].column_letter].width = length + 5
            
    return output.getvalue()

# --- FUNGSI HELPER DATABASE ---

def get_active_session_info():
    """Mengambil GR number sesi aktif saat ini"""
    try:
        res = supabase.table(RECEIVING_TABLE).select("gr_number").eq("is_active", True).limit(1).execute()
        if res.data: return res.data[0]['gr_number']
        return "Belum Ada Sesi Aktif"
    except Exception as e:
        logging.warning(f"Failed to get active session info: {e}")
        return "-"

def get_data(gr_number=None, search_term=None, only_active=True):
    """Mengambil data GR untuk dicek"""
    query = supabase.table(RECEIVING_TABLE).select("*")
    if only_active: query = query.eq("is_active", True)
    elif gr_number: query = query.eq("gr_number", gr_number)
    
    start_time = datetime.now(timezone.utc)
    try:
        response = query.order("nama_barang").execute()
    except Exception as e:
        st.error(f"Gagal mengambil data dari Supabase. Cek RLS: {e}")
        return pd.DataFrame()
        
    df = pd.DataFrame(response.data)

    if 'keterangan' not in df.columns: df['keterangan'] = ""
    if 'jenis' not in df.columns: df['jenis'] = "Stok"

    # Deserialisasi sn_list
    if 'sn_list' in df.columns:
        df['sn_list'] = df['sn_list'].apply(lambda x: json.loads(x) if isinstance(x, str) and x.startswith('[') else (x if isinstance(x, list) else []))

    if not df.empty and search_term:
        df = df[df['nama_barang'].str.contains(search_term, case=False, na=False) | 
                df['sku'].str.contains(search_term, case=False, na=False)]
    
    st.session_state['data_loaded_time'] = start_time
    st.session_state['current_df'] = df.copy()
    
    return df

def get_db_updated_at(id_barang):
    """Mengambil updated_at dari DB saat ini untuk cek konflik"""
    try:
        res = supabase.table(RECEIVING_TABLE).select("updated_at, updated_by").eq("id", id_barang).limit(1).execute()
        if res.data and len(res.data) > 0:
            data = res.data[0]
            return data.get('updated_at'), data.get('updated_by')
        else:
            return datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(), "SYSTEM"
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(), "SYSTEM_ERROR"

# --- FUNGSI ADMIN: PROSES DATA ---

def process_and_insert(df, gr_number):
    """Memproses DF Master GR dan menginput ke DB"""
    data_to_insert = []
    
    required_cols = ['SKU', 'Nama Barang', 'Qty PO', 'Tipe Barang']
    if not all(col in df.columns for col in required_cols):
        return False, f"File Excel harus memiliki kolom: {', '.join(required_cols)}"

    for _, row in df.iterrows():
        jenis_default = row.get('Tujuan (Stok/Display)', 'Stok')
        keterangan_default = row.get('Keterangan Awal', None)

        item = {
            "sku": str(row.get('SKU', '')).strip(),
            "nama_barang": row.get('Nama Barang', 'Unknown Item'),
            "kategori_barang": str(row.get('Tipe Barang', 'NON-SN')).upper(),
            "qty_po": int(row.get('Qty PO', 0)),
            "qty_fisik": 0, "updated_by": "-", "is_active": True, "gr_number": gr_number,
            "jenis": jenis_default,
            "keterangan": keterangan_default,
            "sn_list": [] if str(row.get('Tipe Barang', 'NON-SN')).upper() == 'SN' else None
        }
        data_to_insert.append(item)
    
    if not data_to_insert:
        return False, "Tidak ada data valid untuk diinput."
        
    try:
        # Nonaktifkan sesi lama
        supabase.table(RECEIVING_TABLE).update({"is_active": False}).eq("is_active", True).execute()
        
        # Masukkan data baru
        batch_size = 500
        for i in range(0, len(data_to_insert), batch_size):
            supabase.table(RECEIVING_TABLE).insert(data_to_insert[i:i+batch_size]).execute()
            
        return True, len(data_to_insert)
    except APIError as e:
         return False, f"Gagal API Supabase: {e.message}. Pastikan kolom DB sudah dibuat dengan benar."
    except Exception as e:
         return False, f"Error saat insert data: {str(e)}"

def delete_active_session():
    """Hapus sesi aktif tanpa arsip"""
    try:
        supabase.table(RECEIVING_TABLE).delete().eq("is_active", True).execute()
        return True, "Sesi aktif berhasil dihapus total."
    except Exception as e: return False, str(e)
    
def get_master_template_excel_receiving():
    """Template untuk upload Master GR/PO"""
    data = {
        'SKU': ['SAM-S24-ULT', 'VIV-CBL-01', 'LOG-MOU-05'],
        'Nama Barang': ['Samsung Galaxy S24 Ultra 256GB', 'Vivan Kabel C to C', 'Logitech G502 Hero Mouse'],
        'Qty PO': [10, 500, 25],
        'Tipe Barang': ['SN', 'NON-SN', 'NON-SN'],
        'Tujuan (Stok/Display)': ['Display', 'Stok', 'Stok'],
        'Keterangan Awal': ['Untuk Floor Display', None, None]
    }
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Template_Master_GR')
        worksheet = writer.sheets['Template_Master_GR']
        for column_cells in worksheet.columns:
            length = max(len(str(cell.value) if cell.value else "") for cell in column_cells)
            worksheet.column_dimensions[column_cells[0].column_letter].width = length + 5
    return output.getvalue()


# --- LOGIKA CARD VIEW & UPDATE ---

def handle_update_non_sn(row, new_qty, new_jenis, nama_user, loaded_time, keterangan=""):
    """Update QTY dan Jenis untuk barang NON-SN"""
    id_barang = row['id']
    
    original_row_match = st.session_state['current_df'].loc[st.session_state['current_df']['id'] == id_barang]
    if original_row_match.empty: return 0, True
    original_row = original_row_match.iloc[0]
    
    original_qty = original_row['qty_fisik']
    original_jenis = original_row['jenis']
    original_notes = original_row.get('keterangan', '') if original_row.get('keterangan') is not None else ''
    
    keterangan_to_save = keterangan if keterangan.strip() else None

    is_qty_changed = (original_qty != new_qty)
    is_jenis_changed = (original_jenis != new_jenis)
    is_notes_changed = (original_notes.strip() != (keterangan_to_save.strip() if keterangan_to_save else ''))
    
    if is_qty_changed or is_jenis_changed or is_notes_changed:
        
        # Cek Konflik
        db_updated_at_str, updated_by_db = get_db_updated_at(id_barang)
        db_updated_at = parse_supabase_timestamp(db_updated_at_str)
        if db_updated_at > loaded_time:
            st.error(f"⚠️ KONFLIK DATA: **{row['nama_barang']}**! Diubah oleh **{updated_by_db}** pada {db_updated_at.astimezone(None).strftime('%H:%M:%S')}. Muat Ulang!")
            return 0, True

        # Lakukan Update
        update_payload = {
            "qty_fisik": new_qty, 
            "jenis": new_jenis,
            "updated_at": datetime.utcnow().isoformat(), 
            "updated_by": nama_user,
            "keterangan": keterangan_to_save
        }

        try:
            supabase.table(RECEIVING_TABLE).update(update_payload).eq("id", id_barang).execute()
            return 1, False # Success
        except APIError as api_e:
            st.error(f"❌ Gagal Simpan Item {row['nama_barang']}. Detail: {api_e}")
            return 0, True 
        
    return 0, False # No change

def handle_update_sn_list(row, new_sn_list, new_jenis, nama_user, loaded_time, keterangan=""):
    """Update SN List dan Jenis untuk barang SN"""
    id_barang = row['id']
    
    original_row_match = st.session_state['current_df'].loc[st.session_state['current_df']['id'] == id_barang]
    if original_row_match.empty: return 0, True
    original_row = original_row_match.iloc[0]
    
    original_sn_list = original_row.get('sn_list', [])
    original_jenis = original_row['jenis']
    original_notes = original_row.get('keterangan', '') if original_row.get('keterangan') is not None else ''
    
    keterangan_to_save = keterangan if keterangan.strip() else None

    is_sn_list_changed = set(map(str.strip, original_sn_list)) != set(map(str.strip, new_sn_list))
    is_jenis_changed = (original_jenis != new_jenis)
    is_notes_changed = (original_notes.strip() != (keterangan_to_save.strip() if keterangan_to_save else ''))
    
    if is_sn_list_changed or is_jenis_changed or is_notes_changed:
        
        # Cek Konflik
        db_updated_at_str, updated_by_db = get_db_updated_at(id_barang)
        db_updated_at = parse_supabase_timestamp(db_updated_at_str)
        if db_updated_at > loaded_time:
            st.error(f"⚠️ KONFLIK DATA: **{row['nama_barang']}**! Diubah oleh **{updated_by_db}** pada {db_updated_at.astimezone(None).strftime('%H:%M:%S')}. Muat Ulang!")
            return 0, True

        # Lakukan Update
        update_payload = {
            "sn_list": new_sn_list,
            "qty_fisik": len(new_sn_list), # Qty Fisik = Jumlah SN yang dimasukkan
            "jenis": new_jenis,
            "updated_at": datetime.utcnow().isoformat(), 
            "updated_by": nama_user,
            "keterangan": keterangan_to_save
        }

        try:
            # Gunakan json.dumps untuk memastikan array tersimpan dengan benar di Supabase
            payload_to_db = update_payload.copy()
            payload_to_db['sn_list'] = json.dumps(new_sn_list) 
            
            supabase.table(RECEIVING_TABLE).update(payload_to_db).eq("id", id_barang).execute()
            return 1, False # Success
        except APIError as api_e:
            st.error(f"❌ Gagal Simpan Item SN {row['nama_barang']}. Detail: {api_e}")
            return 0, True 
        
    return 0, False # No change

# --- HALAMAN CHECKER ---
def page_checker():
    active_gr = get_active_session_info()
    st.title(f"📱 Validasi GR: {active_gr}")
    
    if SESSION_KEY_CHECKER not in st.session_state:
        st.session_state[SESSION_KEY_CHECKER] = "-- Pilih Petugas --"
    
    opsi_checker = ["-- Pilih Petugas --"] + DAFTAR_CHECKER
    try:
        default_index = opsi_checker.index(st.session_state[SESSION_KEY_CHECKER])
    except ValueError:
        default_index = 0 

    with st.container():
        c_pemeriksa, c_placeholder = st.columns([1, 3])
        with c_pemeriksa:
            nama_user = st.selectbox("👤 Nama Checker", opsi_checker, index=default_index, key="checker_select")
            if nama_user != st.session_state[SESSION_KEY_CHECKER]:
                 st.session_state[SESSION_KEY_CHECKER] = nama_user
                 st.rerun() 
    
    st.divider()
    final_nama_user = st.session_state[SESSION_KEY_CHECKER]

    if "Pilih Petugas" in final_nama_user:
        st.info("👋 Mohon **pilih nama Anda** terlebih dahulu untuk memulai validasi.")
        st.stop()
        
    if active_gr == "Belum Ada Sesi Aktif":
        st.warning("⚠️ Saat ini belum ada sesi GR/PO yang aktif. Silakan hubungi Admin.")
        st.stop()

    search_txt = st.text_input("🔍 Cari Barang (Ketik SKU/Nama)", placeholder="Contoh: S24 Ultra, Vivan Kabel...")
    
    if st.button("🔄 Muat Ulang Data", key="reload_btn"):
        st.cache_data.clear()
        st.session_state.pop('current_df', None)
        st.rerun()

    df = get_data(gr_number=active_gr, search_term=search_txt, only_active=True)
    loaded_time = st.session_state.get('data_loaded_time', datetime(1970, 1, 1, tzinfo=timezone.utc))
    
    if df.empty:
        st.info(f"Tidak ada data barang yang valid untuk GR **{active_gr}**.")
        return

    df_sn = df[df['kategori_barang'] == 'SN'].copy()
    df_non = df[df['kategori_barang'] == 'NON-SN'].copy()
    
    total_qty_po = df['qty_po'].sum()
    total_qty_fisik_tercatat = df['qty_fisik'].sum()
    progress_percent = total_qty_fisik_tercatat / total_qty_po if total_qty_po > 0 else 0
    
    st.markdown("---")
    col_metric, col_bar = st.columns([1, 3])
    
    with col_metric:
        st.metric("Total Unit Divalidasi", f"{total_qty_fisik_tercatat} / {total_qty_po} (Dari PO)")
    with col_bar:
        st.write("")
        st.caption(f"Progress Total GR: {progress_percent * 100:.1f}%")
        st.progress(progress_percent)
    st.markdown("---")

    # [1] LIST BARANG NON-SN
    if not df_non.empty:
        st.subheader(f"📦 Non-SN ({len(df_non)})")

        for index, row in df_non.iterrows():
            item_id = row['id']
            qty_po = row['qty_po']
            default_qty = row['qty_fisik']
            default_jenis = row['jenis']
            selisih_po = default_qty - qty_po
            
            status_text = "MATCH" if selisih_po == 0 else ("OVER" if selisih_po > 0 else "SHORT")
            status_color = "green" if selisih_po == 0 else "red"
            
            header_text = f"**{row['nama_barang']}** (PO: {qty_po}) | Selisih: :{status_color}[{selisih_po}]"
            
            notes_key = f"notes_non_{item_id}"
            current_notes = row.get('keterangan', '') if row.get('keterangan') is not None else ''
            
            with st.expander(header_text, expanded=selisih_po != 0 and default_qty == 0):
                col_info, col_input = st.columns([1.5, 1.5])
                
                with col_info:
                    st.markdown(f"**SKU:** {row['sku']}")
                    st.markdown(f"**Qty PO (Harapan):** `{qty_po}`")
                    st.markdown(f"**Dicek Oleh:** {row['updated_by']}")
                    if current_notes: st.markdown(f"**Catatan Sebelumnya:** `{current_notes}`")
                
                with col_input:
                    new_qty = st.number_input("JML FISIK DITERIMA", value=default_qty, min_value=0, step=1, key=f"qty_non_{item_id}")
                    
                    new_jenis = st.radio("Tujuan Alokasi", ['Stok', 'Display'], index=['Stok', 'Display'].index(default_jenis), horizontal=True, key=f"jenis_non_{item_id}")
                    
                    keterangan = st.text_area("Keterangan/Isu (Opsional)", value=current_notes, key=notes_key, height=50)

                    if st.button("Simpan Non-SN", key=f"btn_non_{item_id}", type="primary", use_container_width=True):
                        updates, conflict = handle_update_non_sn(row, new_qty, new_jenis, final_nama_user, loaded_time, keterangan.strip())
                        
                        if not conflict and updates > 0:
                            st.toast(f"✅ Qty {row['nama_barang']} ({new_jenis}) disimpan!", icon="💾")
                            time.sleep(0.5)
                            st.rerun()
                        elif not conflict:
                            st.info("Tidak ada perubahan yang tersimpan.")
                            
    st.markdown("---")

    # [2] LIST BARANG SN
    if not df_sn.empty:
        st.subheader(f"📋 SN Items ({len(df_sn)})")
        
        for index, row in df_sn.iterrows():
            item_id = row['id']
            qty_po = row['qty_po']
            current_sn_list = row.get('sn_list', [])
            default_jenis = row['jenis']
            
            qty_fisik = len(current_sn_list)
            selisih_po = qty_fisik - qty_po
            
            status_text = "MATCH" if selisih_po == 0 else ("OVER" if selisih_po > 0 else "SHORT")
            status_color = "green" if selisih_po == 0 else "red"
            
            header_text = f"**{row['nama_barang']}** (PO: {qty_po}) | SN Tercatat: {qty_fisik} | Selisih: :{status_color}[{selisih_po}]"
            
            sn_input_key = f"new_sn_input_{item_id}"
            notes_key = f"notes_sn_{item_id}"
            
            current_notes = row.get('keterangan', '') if row.get('keterangan') is not None else ''

            with st.expander(header_text, expanded=selisih_po != 0):
                
                # --- INPUT SN BARU ---
                st.markdown("##### 📝 Input/Scan Serial Number (SN)")
                col_sn_in, col_sn_jenis, col_sn_add = st.columns([2, 1.5, 1])

                new_sn_input = col_sn_in.text_input("SN", key=sn_input_key, placeholder="Scan atau ketik SN di sini", label_visibility="collapsed").strip()
                
                # Menggunakan default_jenis dari database saat ini sebagai default radio
                radio_jenis_sn_key = f"jenis_sn_input_{item_id}"
                
                if radio_jenis_sn_key not in st.session_state:
                     st.session_state[radio_jenis_sn_key] = default_jenis
                
                new_sn_jenis = col_sn_jenis.radio("Tujuan Alokasi SN", ['Stok', 'Display'], index=['Stok', 'Display'].index(st.session_state[radio_jenis_sn_key]), horizontal=True, key=radio_jenis_sn_key)

                # Logika Penambahan SN
                if col_sn_add.button("➕ Tambah SN", key=f"add_sn_btn_{item_id}", use_container_width=True) and new_sn_input:
                    # Cek duplikasi SN di list yang sudah ada
                    if new_sn_input in current_sn_list:
                        st.warning(f"SN `{new_sn_input}` sudah ada di list.")
                    else:
                        current_sn_list.append(new_sn_input)
                        # Simpan ke DB langsung, agar SN yang ditambahkan oleh Checker lain terlihat
                        updates, conflict = handle_update_sn_list(row, current_sn_list, new_sn_jenis, final_nama_user, loaded_time, current_notes)

                        if not conflict and updates > 0:
                            st.toast(f"✅ SN {new_sn_input} ditambahkan! Total {len(current_sn_list)}")
                            time.sleep(0.5) 
                            st.session_state[sn_input_key] = "" # Clear input
                            st.rerun()
                        elif conflict:
                             st.warning("Gagal menambahkan SN karena ada konflik data.")
                        
                # --- TAMPILAN SN LIST DAN REMOVE ---
                st.markdown("##### 🔍 Daftar SN Tercatat:")
                
                # Tampilkan SN List yang sudah ada di DB (current_sn_list)
                if current_sn_list:
                    for idx, sn in enumerate(current_sn_list):
                        col_sn_disp, col_sn_rem = st.columns([3, 1])
                        col_sn_disp.markdown(f"`{sn}`")
                        
                        # Tombol Hapus SN
                        if col_sn_rem.button("❌ Hapus", key=f"remove_sn_{item_id}_{idx}", use_container_width=True):
                            temp_sn_list = current_sn_list[:]
                            temp_sn_list.pop(idx)
                            
                            # Simpan ke DB
                            updates, conflict = handle_update_sn_list(row, temp_sn_list, default_jenis, final_nama_user, loaded_time, current_notes)

                            if not conflict and updates > 0:
                                st.toast(f"❌ SN {sn} dihapus.")
                                st.rerun()
                            elif conflict:
                                st.warning("Gagal menghapus SN karena ada konflik data.")

                else:
                    st.info("Belum ada Serial Number yang dimasukkan.")
                
                
                st.divider()
                
                # --- SAVE BUTTON FINAL ---
                # Ini untuk update JENIS atau KETERANGAN saja. SN sudah diupdate secara real-time saat ADD/REMOVE.
                keterangan_sn = st.text_area("Keterangan/Isu (Opsional)", value=current_notes, key=notes_key, height=50)

                if st.button("💾 Simpan Keterangan & Alokasi", key=f"btn_sn_{item_id}", type="primary", use_container_width=True):
                    final_jenis = st.session_state[radio_jenis_sn_key]
                    updates, conflict = handle_update_sn_list(row, current_sn_list, final_jenis, final_nama_user, loaded_time, keterangan_sn.strip())
                    
                    if not conflict and updates > 0:
                        st.toast(f"✅ Alokasi dan Keterangan untuk {row['nama_barang']} disimpan!", icon="💾")
                        time.sleep(0.5)
                        st.rerun()
                    elif not conflict:
                        st.info("Tidak ada perubahan Alokasi/Keterangan yang tersimpan.")


# --- FUNGSI ADMIN ---
def page_admin():
    st.title("🛡️ Admin Dashboard (Receiving)")
    active_gr = get_active_session_info()
    
    if active_gr == "Belum Ada Sesi Aktif":
        st.warning("⚠️ Belum ada sesi GR aktif. Silakan mulai sesi baru di bawah.")
    else:
        st.info(f"📅 Sesi Aktif: **{active_gr}**")
    
    tab1, tab2, tab3 = st.tabs(["🚀 Mulai Sesi GR", "🗄️ Laporan & Arsip", "⚠️ Danger Zone"])
    
    with tab1:
        st.markdown("### 1️⃣ Download Template Master GR/PO")
        st.caption("Gunakan template ini untuk menyusun data GR/PO yang akan di-upload.")
        st.download_button("⬇️ Download Template Master GR/PO", get_master_template_excel_receiving(), "Template_Master_Receiving.xlsx")
        
        st.write("---")

        st.markdown("### 2️⃣ Mulai Sesi Penerimaan Baru")
        st.caption("Upload File Master GR/PO di sini. Sesi GR aktif sebelumnya akan diarsipkan.")
        
        gr_number = st.text_input("Nomor GR/PO Baru", placeholder="Contoh: GR/2025/11/001")
        file_master = st.file_uploader("Upload File Master GR/PO", type="xlsx", key="u_main_gr")
        
        if file_master and gr_number:
            if st.button("🔥 MULAI SESI RECEIVING BARU", type="primary"):
                with st.spinner("Mereset & Upload Data GR..."):
                    df = pd.read_excel(file_master)
                    ok, msg = process_and_insert(df, gr_number.strip())
                    if ok: st.success(f"Sesi '{gr_number.strip()}' Dimulai! {msg} data GR masuk."); time.sleep(2); st.cache_data.clear(); st.rerun()
                    else: st.error(f"Gagal: {msg}")


    with tab2:
        st.markdown("### 📊 Laporan Penerimaan")
        mode_view = st.radio("Pilih Data GR:", ["Sesi Aktif Sekarang", "Arsip / History Lama"], horizontal=True)
        df = pd.DataFrame()
        
        if mode_view == "Sesi Aktif Sekarang": 
            df = get_data(only_active=True)
            report_name = active_gr
        else:
            try:
                res = supabase.table(RECEIVING_TABLE).select("gr_number").eq("is_active", False).execute()
                gr_numbers = sorted(list(set([x['gr_number'] for x in res.data])), reverse=True)
                selected_gr = st.selectbox("Pilih Nomor GR Lama:", gr_numbers) if gr_numbers else None
                if selected_gr: 
                    df = get_data(only_active=False, gr_number=selected_gr)
                    report_name = selected_gr
                else: report_name = "Arsip"
            except: st.error("Gagal load history.")

        if not df.empty:
            st.markdown("---")
            df['qty_diff'] = df['qty_fisik'] - df['qty_po']
            
            total_sku = len(df)
            total_po = df['qty_po'].sum()
            total_fisik = df['qty_fisik'].sum()
            total_diff = df['qty_diff'].sum()

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total SKU", total_sku)
            c2.metric("Total Qty PO", total_po)
            c3.metric("Total Qty Fisik", total_fisik)
            c4.metric("Total Selisih", total_diff)
            
            st.dataframe(df[['gr_number', 'sku', 'nama_barang', 'kategori_barang', 'jenis', 'qty_po', 'qty_fisik', 'qty_diff', 'keterangan', 'updated_by', 'updated_at']], use_container_width=True)
            
            st.markdown("### 📥 Download Laporan")
            tgl = datetime.now().strftime('%Y-%m-%d')
            st.download_button(f"📥 Download Laporan {report_name}", convert_df_to_excel(df), f"Laporan_GR_{report_name}_{tgl}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            
            # Tambahkan fungsi arsip sesi yang aktif
            if mode_view == "Sesi Aktif Sekarang" and active_gr != "Belum Ada Sesi Aktif":
                if st.button(f"✅ ARSIPKAN SESI {active_gr}", type="secondary"):
                     try:
                        supabase.table(RECEIVING_TABLE).update({"is_active": False}).eq("gr_number", active_gr).execute()
                        st.success(f"Sesi {active_gr} berhasil diarsipkan!")
                        time.sleep(2); st.rerun()
                     except Exception as e:
                         st.error(f"Gagal mengarsipkan: {e}")


    with tab3:
        st.header("⚠️ DANGER ZONE")
        st.error("Tindakan di sini bersifat permanen.")
        
        st.markdown(f"**Menghapus Sesi Aktif ({active_gr}):** Ini akan menghapus **SELURUH** data sesi GR ini tanpa arsip. Gunakan dengan hati-hati.")
        
        st.divider()
        input_pin = st.text_input("Masukkan PIN Keamanan", type="password", placeholder="PIN Standar: 123456", key="final_pin")
        st.session_state['confirm_reset_state'] = st.checkbox("Saya sadar data sesi ini akan hilang permanen.", key="final_check")
        
        if st.button("🔥 HAPUS SESI GR INI", use_container_width=True):
            if input_pin == RESET_PIN:
                if st.session_state.get('confirm_reset_state', False): 
                    with st.spinner("Menghapus Sesi Aktif..."):
                        ok, msg = delete_active_session()
                        if ok: st.success("Sesi GR berhasil di-reset!"); time.sleep(2); st.rerun()
                        else: st.error(f"Gagal: {msg}")
                else:
                    st.error("Harap centang konfirmasi dulu.")
            else:
                st.error("PIN Salah.")

# --- MAIN ---
def main():
    st.set_page_config(page_title="GR Validation v1.1", page_icon="📦", layout="wide")
    st.sidebar.title("GR Validation Apps v1.1")
    st.sidebar.success(f"Sesi Aktif: {get_active_session_info()}")
    menu = st.sidebar.radio("Navigasi", ["Checker Input", "Admin Panel"])
    if menu == "Checker Input": page_checker()
    elif menu == "Admin Panel":
        pwd = st.sidebar.text_input("Password Admin", type="password")
        if pwd == "admin123": page_admin()

if __name__ == "__main__":
    if SESSION_KEY_CHECKER not in st.session_state:
        st.session_state[SESSION_KEY_CHECKER] = "-- Pilih Petugas --"
    main()
