#include "model.h"
#include "resource.h"
#include "mfc_stubs.h"

BEGIN_MESSAGE_MAP(COrderDlg, CWnd)
    ON_COMMAND(ID_FILE_SAVE, &COrderDlg::OnSave)
    ON_BN_CLICKED(IDC_CANCEL_ORDER, &COrderDlg::OnCancel)
END_MESSAGE_MAP()

void COrderDlg::OnSave() { lastResult = SaveOrder(42); }
void COrderDlg::OnCancel() { lastResult = 0; }

int CallVirtual(OrderService* service) { return service->Commit(5); }
int CallIndirect(int (*callback)(int)) { return callback(5); }

#if 0
// A scanner must not turn this inactive map into a confirmed active relation.
BEGIN_MESSAGE_MAP(COrderDlg, CWnd)
    ON_COMMAND(ID_FILE_SAVE, &COrderDlg::OnCancel)
END_MESSAGE_MAP()
#endif
